"""DataUpdateCoordinator + MQTT handler for DJI Power Station."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import timedelta
from typing import Any

import paho.mqtt.client as mqtt
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DJIPowerAPI, DJIAuthError, DJIAPIError
from .const import (
    DOMAIN,
    MQTT_HOST,
    MQTT_PORT,
    MQTT_KEEPALIVE,
    MQTT_TOPIC_PROPERTY,
    MQTT_TOPIC_EVENTS,
    POLL_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

# Seconds before MQTT token expiry at which we refresh
MQTT_TOKEN_REFRESH_MARGIN = 300  # 5 minutes


class DJIPowerCoordinator(DataUpdateCoordinator):
    """Manages REST polling + MQTT live updates for one DJI power device."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: DJIPowerAPI,
        sn: str,
        device_name: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{sn}",
            update_interval=timedelta(seconds=POLL_INTERVAL),
        )
        self.api = api
        self.sn = sn
        self.device_name = device_name

        # Live state — updated by MQTT or REST poll
        self.state: dict[str, Any] = {}

        # Energy accumulators (kWh) — seeded from HA recorder on startup
        # via DJIPowerEnergySensor.async_added_to_hass, then grown here.
        # _last_energy_ts tracks when we last integrated so we can compute dt.
        self._last_energy_ts: float = 0.0

        # MQTT internals
        self._mqtt_client: mqtt.Client | None = None
        self._mqtt_thread: threading.Thread | None = None
        self._mqtt_token: str | None = None
        self._mqtt_user_uuid: str | None = None
        self._mqtt_token_expires_at: float = 0
        self._mqtt_running = False

    # ------------------------------------------------------------------
    # REST poll (fallback / initial data)
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from REST API (runs every POLL_INTERVAL seconds)."""
        try:
            devices = await self.api.get_devices()
        except DJIAuthError as exc:
            # Raise ConfigEntryAuthFailed so HA shows the re-auth notification
            # and stops hammering the API with bad credentials.
            raise ConfigEntryAuthFailed(
                f"DJI token expired — please re-enter your x-member-token: {exc}"
            ) from exc
        except DJIAPIError as exc:
            raise UpdateFailed(f"API error: {exc}") from exc

        for dev in devices:
            if dev.get("base_info", {}).get("sn") == self.sn:
                base = dev["base_info"]
                # Always update fields that are authoritative in REST
                self.state.setdefault("sn", self.sn)
                self.state["name"] = base.get("name", self.device_name)
                self.state["soc"] = base.get("battery", 0) / 100  # centipercent → %
                self.state["online"] = base.get("online_status", False)
                self.state["device_mode"] = base.get("device_mode")

                # Charging state: prefer MQTT once it has connected.
                # charge_type is often absent from the MQTT payload on this
                # firmware, so fall back to power_in > 5 W as a reliable proxy.
                if "_last_mqtt" in self.state:
                    power_in = self.state.get("power_in", 0) or 0
                    self.state["is_charging"] = power_in > 5
                else:
                    # No MQTT data yet — bootstrap from REST
                    self.state["is_charging"] = bool(base.get("is_charging", False))
                    self.state.setdefault("charge_type", 0)

                # Power / thermal / timing: set defaults only — MQTT owns these
                self.state.setdefault("power_in", 0)
                self.state.setdefault("power_out", 0)
                self.state.setdefault("temperature", None)
                self.state.setdefault("remain_time", None)
                return self.state

        raise UpdateFailed(f"Device {self.sn} not found in device list")

    # ------------------------------------------------------------------
    # MQTT setup / teardown
    # ------------------------------------------------------------------

    async def async_start_mqtt(self) -> None:
        """Start MQTT connection (called from async context)."""
        creds = await self.api.get_mqtt_credentials()
        self._mqtt_token = creds["user_token"]
        self._mqtt_user_uuid = creds["user_uuid"]
        expire = creds.get("expire", 3600)
        self._mqtt_token_expires_at = time.time() + expire - MQTT_TOKEN_REFRESH_MARGIN
        client_id = creds["client_id"]

        _LOGGER.debug("Starting MQTT for %s (client_id=%s)", self.sn, client_id)

        self._mqtt_running = True
        self._start_mqtt_client(client_id, self._mqtt_user_uuid, self._mqtt_token)

        # Schedule periodic token refresh using async_create_task (correct
        # way to schedule a coroutine from within the HA event loop).
        self._schedule_mqtt_token_refresh(expire - MQTT_TOKEN_REFRESH_MARGIN)

    def _start_mqtt_client(self, client_id: str, user_uuid: str, token: str) -> None:
        """Create a fresh paho client and start the background loop thread."""
        # Stop existing client/thread if any
        if self._mqtt_client:
            try:
                self._mqtt_client.disconnect()
                self._mqtt_client.loop_stop()
            except Exception:
                pass
            self._mqtt_client = None

        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv5,
        )
        client.username_pw_set(username=user_uuid, password=token)
        client.tls_set()
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        self._mqtt_client = client

        self._mqtt_thread = threading.Thread(
            target=self._mqtt_loop, daemon=True, name=f"dji_mqtt_{self.sn}"
        )
        self._mqtt_thread.start()

    def _schedule_mqtt_token_refresh(self, delay_seconds: float) -> None:
        """Schedule _async_refresh_mqtt_token to run after delay_seconds.

        Uses call_later + async_create_task — the correct pattern for
        scheduling a coroutine from the HA event loop without involving
        run_coroutine_threadsafe (which is only for cross-thread calls).
        """
        def _fire():
            self.hass.async_create_task(
                self._async_refresh_mqtt_token(),
                name=f"dji_mqtt_refresh_{self.sn}",
            )

        self.hass.loop.call_later(max(delay_seconds, 60), _fire)

    def _mqtt_loop(self) -> None:
        """Run MQTT in background thread."""
        try:
            self._mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=MQTT_KEEPALIVE)
            self._mqtt_client.loop_forever(retry_first_connection=True)
        except Exception as exc:
            _LOGGER.warning("MQTT loop error for %s: %s", self.sn, exc)

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if str(reason_code) == "Success":
            topic_prop = MQTT_TOPIC_PROPERTY.format(sn=self.sn)
            topic_events = MQTT_TOPIC_EVENTS.format(sn=self.sn)
            client.subscribe(topic_prop, qos=0)
            client.subscribe(topic_events, qos=0)
            _LOGGER.info("MQTT connected for %s, subscribed to %s", self.sn, topic_prop)
        else:
            _LOGGER.warning("MQTT connect failed for %s: %s", self.sn, reason_code)

    def _on_message(self, client, userdata, msg) -> None:
        try:
            payload = json.loads(msg.payload.decode())
            method = payload.get("method", "")

            if method == "device_osd":
                host = payload.get("data", {}).get("host", {})
                # Parse in callback thread, then push update dict to HA loop
                update = self._parse_osd_to_dict(host)
                asyncio.run_coroutine_threadsafe(
                    self._async_merge_and_notify(update), self.hass.loop
                )
        except Exception as exc:
            _LOGGER.debug("Error parsing MQTT message: %s", exc)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        if self._mqtt_running:
            _LOGGER.warning(
                "MQTT disconnected for %s (rc=%s) — paho will retry automatically",
                self.sn, reason_code,
            )

    def _parse_osd_to_dict(self, host: dict) -> dict:
        """Parse device_osd host block; returns a partial state dict (thread-safe, no mutation)."""
        update: dict = {"online": True, "_last_mqtt": time.time()}
        battery = host.get("battery", {})
        power_info = host.get("power_info", {})

        if battery:
            charge_pct = battery.get("charge_pct")
            if charge_pct is not None:
                update["soc"] = charge_pct / 100  # centipercent → %

            remain = battery.get("remain_time")
            if remain is not None:
                update["remain_time"] = remain  # seconds

            temp = battery.get("temp")
            if temp is not None:
                update["temperature"] = temp / 100  # centi-°C → °C

            charge_type = battery.get("charge_type")
            if charge_type is not None:
                update["charge_type"] = charge_type
                # NOTE: do NOT derive is_charging from charge_type here.
                # This firmware always sends charge_type=0 in the MQTT payload
                # even when actively charging (confirmed at 600+ W input).

        # AC output state from power_info.interfaces (group_type=2)
        # sw=0 means ON (normal operation), sw=1 means manually OFF
        interfaces = host.get("power_info", {}).get("interfaces", [])
        for iface in interfaces:
            if iface.get("group_type") == 2:  # group_type 2 = AC output
                for item in iface.get("list", []):
                    sw = item.get("sw")
                    if sw is not None:
                        update["ac_output_enabled"] = (sw == 0)  # 0=ON, 1=OFF
                break

        if power_info:
            update["power_in"] = power_info.get("input", 0)   # W
            update["power_out"] = power_info.get("output", 0)  # W

        # Always derive is_charging from power_in — charge_type is unreliable
        # on this firmware (stays 0 even when charging at full speed).
        # A threshold of 5 W avoids false positives from idle draw.
        update["is_charging"] = (update.get("power_in", 0) or 0) > 5

        return update

    def _integrate_energy(self, power_in_w: float, power_out_w: float) -> None:
        """Accumulate energy totals (kWh) from instantaneous power readings (W).

        Called on every MQTT push (~1 s interval).  We skip intervals longer
        than 5 minutes so a reconnect or HA restart never causes a huge spike.
        """
        now = time.time()
        if self._last_energy_ts > 0:
            dt_h = (now - self._last_energy_ts) / 3600.0
            if 0 < dt_h <= 5 / 60:  # ignore gaps > 5 min
                self.state["energy_in"] = round(
                    self.state.get("energy_in", 0.0) + power_in_w * dt_h / 1000,
                    6,
                )
                self.state["energy_out"] = round(
                    self.state.get("energy_out", 0.0) + power_out_w * dt_h / 1000,
                    6,
                )
        self._last_energy_ts = now

    async def _async_merge_and_notify(self, update: dict) -> None:
        """Merge MQTT update into state on the HA event loop and notify listeners."""
        # Integrate energy before the merge so we use current power values
        power_in = float(update.get("power_in", self.state.get("power_in", 0)) or 0)
        power_out = float(update.get("power_out", self.state.get("power_out", 0)) or 0)
        self._integrate_energy(power_in, power_out)

        merged = {**self.state, **update}
        # Energy totals live exclusively in self.state — never let the
        # incoming MQTT payload (which has no energy keys) overwrite them.
        merged["energy_in"] = self.state.get("energy_in", 0.0)
        merged["energy_out"] = self.state.get("energy_out", 0.0)
        self.state = merged
        self.async_set_updated_data(merged)

    async def _async_refresh_mqtt_token(self) -> None:
        """Refresh the MQTT token before it expires."""
        try:
            creds = await self.api.get_mqtt_credentials()
            new_token = creds["user_token"]
            new_client_id = creds["client_id"]
            expire = creds.get("expire", 3600)
            _LOGGER.debug("Refreshed MQTT token for %s (expires in %ds)", self.sn, expire)

            # Replace the MQTT client entirely so new credentials take effect
            # cleanly (avoids race conditions with loop_forever + reconnect).
            self._mqtt_user_uuid = creds["user_uuid"]
            self._mqtt_token = new_token
            self._start_mqtt_client(new_client_id, self._mqtt_user_uuid, new_token)

            self._mqtt_token_expires_at = time.time() + expire - MQTT_TOKEN_REFRESH_MARGIN
            self._schedule_mqtt_token_refresh(expire - MQTT_TOKEN_REFRESH_MARGIN)

        except DJIAuthError as exc:
            _LOGGER.error(
                "MQTT token refresh failed for %s — member token expired: %s. "
                "Re-authenticate in HA Settings → Integrations.",
                self.sn, exc,
            )
        except Exception as exc:
            # Transient error — retry in 5 minutes
            _LOGGER.warning("MQTT token refresh failed for %s: %s — retrying in 5 min", self.sn, exc)
            self._schedule_mqtt_token_refresh(300)

    def publish_ac_output(self, enabled: bool) -> None:
        """Publish AC output command via MQTT (sw=0=ON, sw=1=OFF).

        Called as a fallback when the REST API returns 404.
        Whether the device honours this is firmware-dependent — current testing
        shows the broker ACL blocks user-client publishes to the forward/ topic.
        """
        if not self._mqtt_client:
            return
        import uuid as _uuid
        sw = 0 if enabled else 1
        topic = f"forward/dy/thing/product/{self.sn}/services"
        payload = json.dumps({
            "tid": str(_uuid.uuid4()),
            "bid": str(_uuid.uuid4()),
            "timestamp": int(time.time() * 1000),
            "app_id": "dy301",
            "method": "output_switch",
            "data": {"group_type": 2, "sw": sw},
        })
        self._mqtt_client.publish(topic, payload, qos=1)
        _LOGGER.debug("Published AC output command (sw=%d) to %s", sw, topic)

    async def async_stop_mqtt(self) -> None:
        """Stop MQTT client."""
        self._mqtt_running = False
        if self._mqtt_client:
            self._mqtt_client.disconnect()
            self._mqtt_client.loop_stop()
