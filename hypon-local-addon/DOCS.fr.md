# Hypon Local Bridge

Récupère **localement** les mesures d'un micro-onduleur Hypon (collecteur Wi-Fi
`COM-WF`, onduleur `HMS-800W` et variantes) et les publie dans Home Assistant via
MQTT Discovery — **sans toucher au DNS, ni au DHCP, ni à la box**, et en gardant
l'application constructeur fonctionnelle.

> 🇬🇧 English version: [`DOCS.md`](DOCS.md)

## Comment ça marche

Le collecteur parle MQTT en clair (TCP 1883) vers `hk.hypon.cloud`. Cet add-on,
sur le réseau de l'hôte Home Assistant :

1. **ARP ciblé** — fait croire au **seul micro-onduleur** que la passerelle du
   réseau, c'est Home Assistant. Tout le trafic sortant du module arrive alors sur
   l'hôte HA. Aucun autre appareil n'est touché.
2. **Redirection** — une règle `iptables` chirurgicale envoie le port 1883 du
   module vers un pont local ; tout le reste de son trafic (DNS, NTP…) est
   réacheminé normalement, pour ne rien casser.
3. **Pont** — relaie la connexion octet pour octet vers le vrai cloud Hypon
   (l'appli continue de marcher), décode les trames `invmonitor` et crée
   automatiquement les capteurs dans Home Assistant.
4. **À l'arrêt** — restaure immédiatement le cache ARP du module et supprime
   toutes les règles ajoutées.

## Prérequis importants

- **Réseau « plat » (même segment L2).** L'ARP ne traverse ni routeur ni VLAN.
  Home Assistant et le micro-onduleur doivent être sur le **même sous-réseau**
  (ex. `192.168.1.0/24`). C'est le cas de la très grande majorité des installations
  domestiques. Un répéteur / point d'accès Wi-Fi en pont ne gêne pas.
- **Home Assistant OS / Supervised.** L'add-on a besoin du réseau de l'hôte
  (`host_network`) et des droits `NET_ADMIN` / `NET_RAW`. Il ne peut donc **pas**
  venir du store officiel : il s'installe via un **dépôt d'add-on personnalisé**
  (voir le README du dépôt).
- **Un broker MQTT** (add-on *Mosquitto broker*). L'add-on récupère ses identifiants
  automatiquement ; sinon renseignez-les à la main (voir options).

## Installation

1. Dans Home Assistant : **Paramètres → Modules complémentaires → Boutique →
   menu ⋮ → Dépôts**, ajoutez l'URL du dépôt Git contenant cet add-on.
2. Installez **Hypon Local Bridge**, puis ouvrez l'onglet **Configuration**.
3. Renseignez au minimum `inverter_ip`, démarrez, et surveillez les **Journaux**.

## Options

| Option | Défaut | Rôle |
|---|---|---|
| `inverter_ip` | *(obligatoire)* | IP du collecteur Wi-Fi du micro-onduleur |
| `gateway_ip` | *(auto)* | IP de la passerelle usurpée vis-à-vis du module ; vide = passerelle par défaut détectée |
| `interface` | *(auto)* | Interface réseau de l'hôte ; vide = détectée via la route par défaut |
| `relay_to_cloud` | `true` | Relaie vers le cloud Hypon (laisse l'appli fonctionner). `false` = 100 % local |
| `cloud_host` | `hk.hypon.cloud` | Hôte du cloud Hypon vers lequel relayer. À ne changer que si besoin |
| `cloud_port` | `1883` | Port du cloud |
| `listen_port` | `18883` | Port local du pont (cible de la redirection). **Différent de 1883** pour ne pas entrer en conflit avec Mosquitto |
| `base_topic` | `hypon` | Préfixe des topics d'état publiés |
| `discovery_prefix` | `homeassistant` | Préfixe MQTT Discovery |
| `publish_raw` | `false` | Publie aussi les trames brutes en hex (`hypon/raw/*`) pour poursuivre le décodage |
| `mqtt_host` | *(service HA)* | Broker MQTT manuel ; vide = fourni par Home Assistant |
| `mqtt_port` / `mqtt_user` / `mqtt_password` | — | Identifiants MQTT manuels (si `mqtt_host` renseigné) |
| `debug` | `false` | Journaux détaillés |

### À propos de `gateway_ip` (« l'adresse cible »)

C'est l'adresse que l'add-on **usurpe auprès du module** : le micro-onduleur croira
que cette IP (normalement votre box, ex. `192.168.1.254`) se trouve sur la carte
réseau de Home Assistant. Laissée vide, elle est détectée automatiquement à partir
de la route par défaut de l'hôte. Ne la changez que si votre passerelle réelle n'est
pas celle détectée.

## Vérifier que ça marche

- **Journaux** de l'add-on : vous devez voir `interface …`, `inverter MAC …`,
  `netfilter: :1883 from … -> local :18883`, puis des trames `invmonitor`.
- **Paramètres → Appareils et services → MQTT** : un appareil *Hypontech* avec ses
  capteurs (puissance, tensions/courants PV, énergie…) apparaît tout seul.

## Dépannage

- **`inverter … did not answer ARP`** : mauvaise IP, ou HA n'est pas sur le même
  segment L2 que le module (VLAN/routeur entre les deux).
- **Les capteurs n'apparaissent pas** mais les trames arrivent : problème MQTT. En
  `host_network`, le nom `core-mosquitto` n'est pas toujours résolu ; renseignez
  `mqtt_host` avec l'IP LAN de votre Home Assistant.
- **Conflit de port** au démarrage du pont : changez `listen_port` (gardez-le
  différent de 1883).

## Sécurité et légalité

L'add-on n'agit que sur **votre** matériel, sur **votre** réseau, et vise **la seule
IP du micro-onduleur**. Il ne s'attaque pas au cloud : il ne fait que relayer le
trafic que le module émet déjà. À l'arrêt, le réseau revient exactement à son état
initial.
