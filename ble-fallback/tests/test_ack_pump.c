#include "ack_pump.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

enum { TEST_BUSY = 7, TEST_NOMEM = 8, TEST_FATAL = 9, TEST_TIMEOUT = 10 };

typedef struct {
    int next_rc;
    int calls;
    int accepted;
    int synchronous_rc;
    bool complete_synchronously;
    ack_cell *callbacks[8];
    unsigned char submitted[8][32];
    size_t lengths[8];
} submit_state;

static int fake_submit(void *arg, const void *bytes, size_t n, ack_cell *cell)
{
    submit_state *s = arg;
    int index = s->calls++;

    assert(index < 8);
    assert(n <= sizeof(s->submitted[index]));
    memcpy(s->submitted[index], bytes, n);
    s->lengths[index] = n;

    int rc = s->next_rc;
    s->next_rc = 0;
    if (rc != 0)
        return rc;

    s->accepted++;
    s->callbacks[index] = cell;
    if (s->complete_synchronously) {
        s->callbacks[index] = NULL;
        ack_complete(cell, s->synchronous_rc);
    }
    return 0;
}

static void assert_submission(const submit_state *s, int index,
                             const void *bytes, size_t n)
{
    assert(index < s->calls);
    assert(s->lengths[index] == n);
    assert(memcmp(s->submitted[index], bytes, n) == 0);
}

static void test_busy_and_nomem_retry_identical_bytes(void)
{
    const unsigned char payload[] = { 0x00, 0x42, 0xff, 0x19 };
    submit_state s = { .next_rc = TEST_BUSY };
    ack_pump p = {0};

    assert(ack_step(&p, 100, 20, payload, sizeof(payload), fake_submit,
                    &s, TEST_BUSY, TEST_NOMEM, TEST_TIMEOUT) == 0);
    assert(!p.active && !p.failed && s.calls == 1 && s.accepted == 0);
    assert_submission(&s, 0, payload, sizeof(payload));

    s.next_rc = TEST_NOMEM;
    assert(ack_step(&p, 101, 20, payload, sizeof(payload), fake_submit,
                    &s, TEST_BUSY, TEST_NOMEM, TEST_TIMEOUT) == 0);
    assert(!p.active && !p.failed && s.calls == 2 && s.accepted == 0);
    assert_submission(&s, 1, payload, sizeof(payload));

    assert(ack_step(&p, 102, 20, payload, sizeof(payload), fake_submit,
                    &s, TEST_BUSY, TEST_NOMEM, TEST_TIMEOUT) == 0);
    assert(p.active && s.calls == 3 && s.accepted == 1);
    assert_submission(&s, 2, payload, sizeof(payload));
    ack_complete(s.callbacks[2], 0);
    s.callbacks[2] = NULL;
    assert(ack_step(&p, 103, 20, payload, sizeof(payload), fake_submit,
                    &s, TEST_BUSY, TEST_NOMEM, TEST_TIMEOUT) == 1);
    assert(!p.active && !p.failed && s.calls == 3);
}

static void test_synchronous_callback(void)
{
    const char payload[] = "sync";
    submit_state s = { .complete_synchronously = true, .synchronous_rc = 0 };
    ack_pump p = {0};

    assert(ack_step(&p, 1, 10, payload, sizeof(payload), fake_submit, &s,
                    TEST_BUSY, TEST_NOMEM, TEST_TIMEOUT) == 1);
    assert(!p.active && !p.failed && s.calls == 1 && s.accepted == 1);
    assert_submission(&s, 0, payload, sizeof(payload));
}

static void test_delayed_ack(void)
{
    const char payload[] = "delayed";
    submit_state s = {0};
    ack_pump p = {0};

    assert(ack_step(&p, 40, 5, payload, sizeof(payload), fake_submit, &s,
                    TEST_BUSY, TEST_NOMEM, TEST_TIMEOUT) == 0);
    assert(p.active && p.started == 40 && s.calls == 1);
    assert(ack_step(&p, 44, 5, payload, sizeof(payload), fake_submit, &s,
                    TEST_BUSY, TEST_NOMEM, TEST_TIMEOUT) == 0);
    assert(p.active && s.calls == 1);
    ack_complete(s.callbacks[0], 0);
    s.callbacks[0] = NULL;
    assert(ack_step(&p, 45, 5, payload, sizeof(payload), fake_submit, &s,
                    TEST_BUSY, TEST_NOMEM, TEST_TIMEOUT) == 1);
    assert(!p.active && !p.failed && s.calls == 1);
}

static void test_reset_stale_callback_same_handle_replacement(void)
{
    const char old_payload[] = "old";
    const char new_payload[] = "new";
    submit_state s = {0};
    ack_pump p = {0};

    assert(ack_step(&p, 10, 50, old_payload, sizeof(old_payload), fake_submit,
                    &s, TEST_BUSY, TEST_NOMEM, TEST_TIMEOUT) == 0);
    ack_cell *stale = s.callbacks[0];
    assert(p.active == stale);
    ack_reset(&p);
    assert(!p.active && !p.failed);

    assert(ack_step(&p, 11, 50, new_payload, sizeof(new_payload), fake_submit,
                    &s, TEST_BUSY, TEST_NOMEM, TEST_TIMEOUT) == 0);
    ack_cell *replacement = s.callbacks[1];
    assert(replacement != stale);
    assert(p.active == replacement && s.calls == 2);

    ack_complete(stale, TEST_FATAL);
    s.callbacks[0] = NULL;
    assert(p.active == replacement && !p.failed);
    assert(ack_step(&p, 12, 50, new_payload, sizeof(new_payload), fake_submit,
                    &s, TEST_BUSY, TEST_NOMEM, TEST_TIMEOUT) == 0);
    assert(p.active == replacement && s.calls == 2);

    ack_complete(replacement, 0);
    s.callbacks[1] = NULL;
    assert(ack_step(&p, 13, 50, new_payload, sizeof(new_payload), fake_submit,
                    &s, TEST_BUSY, TEST_NOMEM, TEST_TIMEOUT) == 1);
    assert(!p.active && !p.failed);
}

static void test_timeout_is_bounded_and_does_not_resend(void)
{
    const char payload[] = "once";
    submit_state s = {0};
    ack_pump p = {0};

    assert(ack_step(&p, 100, 4, payload, sizeof(payload), fake_submit, &s,
                    TEST_BUSY, TEST_NOMEM, TEST_TIMEOUT) == 0);
    ack_cell *late = s.callbacks[0];
    assert(ack_step(&p, 103, 4, payload, sizeof(payload), fake_submit, &s,
                    TEST_BUSY, TEST_NOMEM, TEST_TIMEOUT) == 0);
    assert(ack_step(&p, 104, 4, payload, sizeof(payload), fake_submit, &s,
                    TEST_BUSY, TEST_NOMEM, TEST_TIMEOUT) == -1);
    assert(!p.active && p.failed && p.reason == TEST_TIMEOUT && s.calls == 1);

    assert(ack_step(&p, 105, 4, payload, sizeof(payload), fake_submit, &s,
                    TEST_BUSY, TEST_NOMEM, TEST_TIMEOUT) == -1);
    assert(s.calls == 1);
    ack_complete(late, 0);
    s.callbacks[0] = NULL;
    assert(p.failed && p.reason == TEST_TIMEOUT && s.calls == 1);
}

static void test_terminal_failure(void)
{
    const char payload[] = "failure";
    submit_state s = {0};
    ack_pump p = {0};

    assert(ack_step(&p, 7, 20, payload, sizeof(payload), fake_submit, &s,
                    TEST_BUSY, TEST_NOMEM, TEST_TIMEOUT) == 0);
    ack_complete(s.callbacks[0], TEST_FATAL);
    s.callbacks[0] = NULL;
    assert(ack_step(&p, 8, 20, payload, sizeof(payload), fake_submit, &s,
                    TEST_BUSY, TEST_NOMEM, TEST_TIMEOUT) == -1);
    assert(!p.active && p.failed && p.reason == TEST_FATAL && s.calls == 1);
    assert(ack_step(&p, 9, 20, payload, sizeof(payload), fake_submit, &s,
                    TEST_BUSY, TEST_NOMEM, TEST_TIMEOUT) == -1);
    assert(s.calls == 1);
}

static void test_terminal_submit_failure(void)
{
    const char payload[] = "submit-failure";
    submit_state s = { .next_rc = TEST_FATAL };
    ack_pump p = {0};

    assert(ack_step(&p, 1, 20, payload, sizeof(payload), fake_submit, &s,
                    TEST_BUSY, TEST_NOMEM, TEST_TIMEOUT) == -1);
    assert(!p.active && p.failed && p.reason == TEST_FATAL && s.calls == 1);
}

int main(void)
{
    test_busy_and_nomem_retry_identical_bytes();
    test_synchronous_callback();
    test_delayed_ack();
    test_reset_stale_callback_same_handle_replacement();
    test_timeout_is_bounded_and_does_not_resend();
    test_terminal_failure();
    test_terminal_submit_failure();
    puts("ack_step tests passed (7 cases)");
    return 0;
}
