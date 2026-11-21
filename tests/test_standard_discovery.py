"""Guard production NimBLE standard discovery responders against removal."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
BLE_DEFAULTS = ROOT / "k151-standalone/sdkconfig.ble.defaults"
RADIO = ROOT / "k151-standalone/main/ble_radio.c"


class StandardDiscoveryTest(unittest.TestCase):
    def test_production_ble_config_keeps_client_and_standard_server(self):
        config = BLE_DEFAULTS.read_text(encoding="utf-8")
        for setting in (
            "CONFIG_BT_NIMBLE_ENABLED=y",
            "CONFIG_BT_NIMBLE_ROLE_CENTRAL=y",
            "CONFIG_BT_NIMBLE_ROLE_PERIPHERAL=y",
            "CONFIG_BT_NIMBLE_GAP_SERVICE=y",
            "CONFIG_BT_NIMBLE_ROLE_BROADCASTER=n",
        ):
            with self.subTest(setting=setting):
                self.assertIn(setting, config)

    def test_ble_client_registers_standard_services_before_host_start(self):
        source = RADIO.read_text(encoding="utf-8")
        init = source.index("nimble_port_init()")
        gap_init = source.index("ble_svc_gap_init()", init)
        gatt_init = source.index("ble_svc_gatt_init()", gap_init)
        start_host = source.index("nimble_port_freertos_init(host_task)", gatt_init)
        self.assertLess(init, gap_init)
        self.assertLess(gap_init, gatt_init)
        self.assertLess(gatt_init, start_host)
        self.assertIn("ble_hs_synced()", source)
        self.assertNotIn("ble_gap_adv_start(", source)
        self.assertNotIn("ble_gap_disc(", source)
        self.assertNotIn("ble_gatts_add_svcs(", source)


if __name__ == "__main__":
    unittest.main()
