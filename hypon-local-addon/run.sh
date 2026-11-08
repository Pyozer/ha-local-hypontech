#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -e

# --- interception ---------------------------------------------------------
export HYPON_INV_IP="$(bashio::config 'inverter_ip')"
export HYPON_GW_IP="$(bashio::config 'gateway_ip')"
export HYPON_IFACE="$(bashio::config 'interface')"
export HYPON_LISTEN_PORT="$(bashio::config 'listen_port')"

# --- pont / cloud ---------------------------------------------------------
if bashio::config.true 'relay_to_cloud'; then
    export HYPON_UPSTREAM="on"
else
    export HYPON_UPSTREAM="off"
fi
export HYPON_UPSTREAM_HOST="$(bashio::config 'cloud_host')"
export HYPON_UPSTREAM_PORT="$(bashio::config 'cloud_port')"
export HYPON_BASE_TOPIC="$(bashio::config 'base_topic')"
export HA_DISCOVERY_PREFIX="$(bashio::config 'discovery_prefix')"
if bashio::config.true 'publish_raw'; then
    export HYPON_PUBLISH_RAW="on"
else
    export HYPON_PUBLISH_RAW="off"
fi
if bashio::config.true 'debug'; then
    export HYPON_DEBUG="1"
fi

# --- MQTT : réglage manuel sinon service HA -------------------------------
if bashio::config.has_value 'mqtt_host'; then
    export HA_MQTT_HOST="$(bashio::config 'mqtt_host')"
    export HA_MQTT_PORT="$(bashio::config 'mqtt_port')"
    export HA_MQTT_USER="$(bashio::config 'mqtt_user')"
    export HA_MQTT_PASSWORD="$(bashio::config 'mqtt_password')"
    bashio::log.info "MQTT: broker manuel ${HA_MQTT_HOST}:${HA_MQTT_PORT}"
elif bashio::services.available 'mqtt'; then
    svc_host="$(bashio::services 'mqtt' 'host')"
    # host_network : 'core-mosquitto' n'est pas toujours résoluble -> repli 127.0.0.1
    if ! getent hosts "${svc_host}" >/dev/null 2>&1; then
        bashio::log.warning "'${svc_host}' non résoluble en host_network, repli sur 127.0.0.1"
        svc_host="127.0.0.1"
    fi
    export HA_MQTT_HOST="${svc_host}"
    export HA_MQTT_PORT="$(bashio::services 'mqtt' 'port')"
    export HA_MQTT_USER="$(bashio::services 'mqtt' 'username')"
    export HA_MQTT_PASSWORD="$(bashio::services 'mqtt' 'password')"
    bashio::log.info "MQTT: service HA ${HA_MQTT_HOST}:${HA_MQTT_PORT}"
else
    bashio::log.warning "Aucun broker MQTT : les capteurs ne seront pas publiés (relais cloud seul)."
fi

if ! bashio::config.has_value 'inverter_ip'; then
    bashio::exit.nok "Configurez 'inverter_ip' (IP du micro-onduleur) avant de démarrer."
fi

bashio::log.info "Démarrage de l'interception Hypon pour ${HYPON_INV_IP}"
exec python3 /app/hypon_intercept.py
