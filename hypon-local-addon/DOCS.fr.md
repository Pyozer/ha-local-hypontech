# Hypon Local Bridge

Récupère **localement** les mesures d'un micro-onduleur Hypon (collecteur Wi-Fi
`COM-WF`, onduleur `HMS-800W` et variantes) et les publie dans Home Assistant via
MQTT Discovery — **sans modifier le DNS ni les réglages de votre box** — et en gardant
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
3. Renseignez au minimum `inverter_ips`, démarrez, et surveillez les **Journaux**.

### Trouver l'IP de l'onduleur

Cherchez en journée (l'onduleur est éteint la nuit) : dans la liste des appareils de
votre box (`Espressif`, `Hypon` ou `COM-WF`), ou avec la commande de scan d'une ligne
du [README du dépôt](https://github.com/Pyozer/ha-local-hypontech/blob/main/README.fr.md#démarrage-rapide).

Ensuite, **réservez cette IP à l'onduleur dans les réglages DHCP de votre box** (bail
statique) pour qu'elle ne change jamais. Si l'add-on affiche en boucle
`does not answer ARP` en plein jour, l'IP a probablement changé : retrouvez-la.

### Plusieurs micro-onduleurs

Listez toutes leurs IP dans `inverter_ips`, séparées par des virgules (ex.
`192.168.1.87, 192.168.1.88` ; puis redémarrez l'add-on. Chaque micro-onduleur obtient son
propre appareil *Hypontech* dans Home Assistant, identifié par son numéro de série :
rien n'est mélangé. En ajouter un ne change pas les entités des autres, et l'un qui
s'endort n'affecte pas les autres. Tous doivent être sur le même réseau que Home
Assistant.

## Options

| Option | Défaut | Rôle |
|---|---|---|
| `inverter_ips` | `192.168.1.` | IP du collecteur Wi-Fi du micro-onduleur, ou plusieurs séparées par des virgules (voir *Plusieurs micro-onduleurs*). Pré-rempli avec le sous-réseau habituel : il suffit d'ajouter le dernier nombre (ex. `192.168.1.87`) |
| `gateway_ip` | *(auto)* | IP de la passerelle usurpée vis-à-vis du module ; vide = passerelle par défaut détectée |
| `interface` | *(auto)* | Interface réseau de l'hôte ; vide = détectée via la route par défaut |
| `relay_to_cloud` | `true` | Relaie vers le cloud Hypon (laisse l'appli fonctionner). `false` = 100 % local |
| `cloud_host` | `hk.hypon.cloud` | Hôte du cloud Hypon vers lequel relayer. À ne changer que si besoin |
| `cloud_port` | `1883` | Port du cloud |
| `listen_port` | `18883` | Port local du pont (cible de la redirection). **Différent de 1883** pour ne pas entrer en conflit avec Mosquitto |
| `base_topic` | `hypon` | Préfixe des topics d'état publiés |
| `discovery_prefix` | `homeassistant` | Préfixe MQTT Discovery |
| `publish_raw` | `false` | Publie aussi les trames brutes en hex (`hypon/raw/*`) pour poursuivre le décodage |
| `offline_grace` | `300` | Secondes de silence tolérées avant de regarder le réseau pour conclure (éteint ou perdu, voir ci-dessous). Un onduleur qui répond lentement obtient automatiquement plus de temps. `0` = immédiat |
| `mqtt_host` | *(service HA)* | Broker MQTT manuel ; vide = fourni par Home Assistant |
| `mqtt_port` / `mqtt_user` / `mqtt_password` | — | Identifiants MQTT manuels (si `mqtt_host` renseigné) |
| `debug` | `false` | Journaux détaillés |

### À propos de `gateway_ip` (« l'adresse cible »)

C'est l'adresse que l'add-on **usurpe auprès du module** : le micro-onduleur croira
que cette IP (normalement votre box, ex. `192.168.1.254`) se trouve sur la carte
réseau de Home Assistant. Laissée vide, elle est détectée automatiquement à partir
de la route par défaut de l'hôte. Ne la changez que si votre passerelle réelle n'est
pas celle détectée.

### Nuit, silence et disponibilité

Le micro-onduleur et son collecteur Wi-Fi sont alimentés par les panneaux : sans lumière
(nuit, orage, ombrage important) ils sont éteints et ne disent rien. Un onduleur dont
l'adresse IP a changé ne dit rien non plus. Le silence seul est ambigu, et la position du
soleil ne trancherait pas (le soleil est « levé » pendant un orage). Le **réseau**, lui,
tranche : un onduleur alimenté répond à l'ARP, à son IP ou, si son adresse a changé, à une
autre, toujours avec la même adresse MAC ; un onduleur éteint n'est visible nulle part.
Après `offline_grace` secondes sans trame, l'add-on regarde, et donne à chaque onduleur un
**statut** :

| Statut | Signification | Entités |
|---|---|---|
| `online` | Des trames arrivent | Valeurs en direct |
| `sleeping` | Silencieux et **absent du réseau** : éteint faute de lumière | Disponibles, production à **0**, totaux conservés |
| `unreachable` | Il répond au réseau mais n'envoie rien à l'add-on | **Indisponibles** |
| `ip_changed` | Silencieux, et son adresse MAC a été vue sous **une autre IP** | **Indisponibles** |

Ainsi, la nuit, ou sous un orage en plein jour, l'onduleur affiche **0 W** et jamais
« indisponible ». Il ne devient indisponible que lorsqu'il y a une preuve positive qu'il
est vivant mais pas joignable comme configuré.

Comment le réseau est observé : toutes les 30 secondes, l'add-on demande à chaque onduleur
« es-tu là ? » (ARP). Tant que l'un ne répond pas, il demande aussi à **chaque adresse de
votre réseau local** (une requête ARP chacune) qui elle est, pour retrouver l'adresse MAC
de cet onduleur où qu'il soit passé. Il le fait tout de suite, puis après 30 secondes,
1 minute et 2 minutes, puis seulement toutes les 5 minutes : une nuit de silence coûte une
recherche toutes les 5 minutes. Cela représente quelques kilo-octets, uniquement pendant
que l'onduleur est silencieux, et rien ne sort de votre réseau. Dès que l'onduleur répond
à nouveau, la séquence repart de zéro.

Le rythme habituel d'un onduleur est appris : un onduleur qui répond lentement obtient
trois fois sa période (jamais moins que `offline_grace`). Le collecteur se déconnecte et se
reconnecte souvent autour du coucher du soleil : les reconnexions dans le délai ne
changent rien, ni une connexion coupée à elle seule, seul le silence qui suit compte.

**Pourquoi indisponible et pas 0 W quand il est perdu ?** Un `0` affirmerait que l'onduleur
ne produit rien, ce qui serait faux. `indisponible` dit honnêtement qu'on ne sait pas, et
ne laisse jamais une valeur périmée (comme la dernière production avant une coupure de
courant) à l'écran. Les totaux ne sont pas remis à zéro : ils ne font qu'augmenter et la
première trame après la panne ramène la bonne valeur.

**Limites.** « Éteint » est déduit de l'absence de l'onduleur sur le réseau : un onduleur
qui produit mais qu'on **ne voit pas du tout** (son Wi-Fi est coupé, par exemple, ou il est
sur un autre réseau) affiche donc 0 W. L'add-on ne peut garantir que ce que le réseau lui
montre. Il lui faut aussi l'adresse MAC de l'onduleur, apprise la première fois qu'il
l'intercepte ; sans elle (ou si la surveillance ARP ne peut pas tourner), un onduleur
silencieux est déclaré `unreachable` plutôt que de deviner 0 W.

Tout reprend à la première trame. L'add-on se souvient des onduleurs qu'il servait
(`/data`) : après un redémarrage ou une coupure de courant, il ne continue donc jamais
d'afficher une valeur que plus personne ne confirme. Un onduleur qui était en ligne
dispose d'un délai complet pour se manifester à nouveau, puis les mêmes règles
s'appliquent.

#### Entités

Chaque onduleur est un appareil *Hypontech* avec ses mesures (puissance, tension /
courant / puissance PV, réseau, température, énergie) et ces entités de
**diagnostic**, qui restent disponibles même quand les mesures ne le sont pas, pour
expliquer pourquoi :

| Entité | Contenu |
|---|---|
| **Statut** | `online`, `sleeping`, `unreachable` ou `ip_changed` |
| **Connexion** | *on* uniquement quand l'onduleur est `online` |
| **Dernière trame** | Quand la dernière trame est arrivée (horodatage) |
| **Adresse IP** | Où se trouve réellement l'onduleur : l'adresse depuis laquelle il parle, ou sa nouvelle après un `ip_changed` |
| **Adresse IP configurée** | L'adresse où l'add-on l'attend (celle de `inverter_ips`) |
| **Adresse MAC** | Son adresse matérielle (désactivée par défaut, activez-la si besoin) |

Pour être prévenu quand l'adresse change, une automatisation peut se déclencher quand
**Statut** passe à `ip_changed`, ou comparer les deux adresses, par exemple avec la
condition template
`{{ states('sensor.hypon_ip_address') != states('sensor.hypon_configured_ip') }}`
(adaptez les identifiants d'entités aux vôtres), et mettre la nouvelle adresse dans la
notification.

## Vérifier que ça marche

- **Journaux** de l'add-on : vous devez voir `interface …`, `inverter MAC …`,
  `netfilter: :1883 from … -> local :18883`, puis des trames `invmonitor`.
- **Paramètres → Appareils et services → MQTT** : un appareil *Hypontech* avec ses
  capteurs (puissance, tensions/courants PV, énergie…) apparaît tout seul.

## Dépannage

- **`inverter … does not answer ARP`** : normal la nuit, l'onduleur est éteint et
  l'add-on l'attend. Si le message persiste en plein jour : mauvaise IP, ou HA n'est
  pas sur le même segment L2 que le module (VLAN/routeur entre les deux).
- **Statut `ip_changed`** : l'onduleur a reçu une autre adresse (après une coupure de
  courant, par exemple). Lisez-la dans le capteur **Adresse IP**, renseignez-la dans
  `inverter_ips`, redémarrez, et réservez-lui une adresse fixe dans votre box pour
  qu'elle ne bouge plus.
- **Statut `unreachable`** : l'onduleur est sur le réseau mais son trafic n'atteint plus
  l'add-on. Redémarrez l'add-on pour refaire l'interception, et vérifiez que
  `inverter_ips` est correct.
- **0 W alors que l'onduleur produit** : l'add-on ne le voit pas du tout sur le réseau
  (voir *Limites* ci-dessus). Vérifiez son Wi-Fi et qu'il est sur le même réseau que
  Home Assistant.
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

**Produit non officiel.** Cet add-on est un projet communautaire indépendant, ni
affilié à Hypontech, ni approuvé ou soutenu par elle. Les noms de produits et
d'entreprises appartiennent à leurs propriétaires respectifs et ne sont utilisés que
pour indiquer les appareils compatibles.
