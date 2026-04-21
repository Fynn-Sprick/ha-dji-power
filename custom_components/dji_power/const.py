"""Constants for DJI Power Station integration."""

DOMAIN = "dji_power"
MANUFACTURER = "DJI"

# API
HOME_API = "https://home-api-vg.djigate.com"
HOME_API_FALLBACK = "https://home-api.djigate.com"
DEVICES_LIST_PATH = "/app/api/v1/users/devices/list"
MQTT_TOKEN_PATH = "/app/api/v1/users/auth/token?reason=mqtt"
WELCOME_REGION_PATH = "/app/api/v1/users/welcome/region"

# MQTT
MQTT_HOST = "crobot-mqtt-us.djigate.com"
MQTT_PORT = 8883
MQTT_KEEPALIVE = 60
# Topic: forward/dy/thing/product/{sn}/property  (device_osd messages every ~1 sec)
MQTT_TOPIC_PROPERTY = "forward/dy/thing/product/{sn}/property"
MQTT_TOPIC_EVENTS = "forward/dy/thing/product/{sn}/events"

# Config keys
CONF_MEMBER_TOKEN = "member_token"
CONF_SN = "sn"
CONF_DEVICE_NAME = "device_name"
CONF_REGION = "region"

# Update intervals
POLL_INTERVAL = 60  # seconds — REST poll as fallback

# Sensor names / keys
SENSOR_SOC = "soc"
SENSOR_POWER_IN = "power_in"
SENSOR_POWER_OUT = "power_out"
SENSOR_TEMP = "temperature"
SENSOR_REMAIN_TIME = "remaining_time"
SENSOR_ONLINE = "online"
SENSOR_CHARGING = "charging"
SENSOR_CHARGE_TYPE = "charge_type"

# Device types
DEVICE_TYPE_NAMES = {
    271: "DJI Power 1000 V2",
    270: "DJI Power 1000",
    275: "DJI Power 500",
    # Extend as more models are discovered
}

# Charge types
CHARGE_TYPE_MAP = {
    0: "not_charging",
    1: "ac",
    2: "solar",
    3: "car",
    4: "dc",
}
