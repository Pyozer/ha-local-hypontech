# LocalHyponTech — dépôt d'add-ons Home Assistant

[English](README.md) · **Français**

Dépôt d'add-on personnalisé pour Home Assistant contenant **Hypon Local Bridge** :
lecture **locale** des données d'un micro-onduleur Hypon (collecteur `COM-WF`,
`HMS-800W` et variantes), **sans modifier le DNS ni les réglages de votre box**, avec
publication automatique dans Home Assistant (MQTT Discovery) et relais transparent vers
le cloud Hypon (l'appli constructeur continue de fonctionner).

## Prérequis

- Home Assistant OS / Supervised.
- HA et le micro-onduleur sur le **même réseau** (même sous-réseau, sans VLAN
  ni routeur entre les deux).
- Un broker MQTT (add-on Mosquitto) — identifiants récupérés automatiquement.

## Démarrage rapide

### 1. Ajouter ce dépôt à Home Assistant

[![Ouvrez votre instance Home Assistant avec ce dépôt pré-rempli dans la fenêtre d'ajout d'un dépôt d'add-ons.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FPyozer%2Fha-local-hypontech)

Ou ajoutez-le à la main : **Paramètres → Modules complémentaires → Boutique → menu ⋮
→ Dépôts**, puis ajoutez :

```
https://github.com/Pyozer/ha-local-hypontech
```

### 2. Trouver l'adresse IP de l'onduleur, et la fixer

Le module Wi-Fi de l'onduleur reçoit son IP de votre box. **Faites-le en journée** :
l'onduleur est alimenté par les panneaux et s'éteint la nuit.

**Option A — votre box (le plus simple).** Dans son interface ou son appli, ouvrez la
liste des appareils connectés (ou baux DHCP). L'onduleur apparaît en général sous le nom
`Espressif`, `Hypon` ou `COM-WF`, parfois sans nom du tout.

**Option B — scanner votre réseau.** L'onduleur sert une petite page sur `/local_web`.
Cette commande affiche l'IP de l'appareil qui répond avec la page Hypon (remplacez la
valeur de `NET` par votre sous-réseau s'il n'est pas `192.168.1`) :

```bash
NET=192.168.1; (for i in $(seq 1 254); do (curl -s -m 2 "http://$NET.$i/local_web" | grep -q Hypon && echo "Onduleur : $NET.$i") & done; wait)
```

Elle fonctionne sur macOS, Linux, et dans l'add-on **Terminal & SSH** de Home
Assistant. Pour vérifier, ouvrez `http://<ip>/local_web` : vous devez voir *Device
Information* avec le modèle `COM-WF`.

> **macOS :** si un navigateur n'arrive pas à joindre l'adresse alors que `curl` y
> arrive, autorisez-le dans **Réglages Système → Confidentialité et sécurité → Réseau
> local**.

**Réservez ensuite cette IP pour l'onduleur** (recommandé, sinon l'add-on le perd si la
box lui attribue une nouvelle adresse). Dans les réglages DHCP de votre box, cherchez
*réservation DHCP*, *bail statique* ou *association IP/MAC* et choisissez l'onduleur
grâce à son adresse MAC. Faites-le sur la box, pas sur l'onduleur.

### 3. Installer et démarrer

Dans la Boutique, ouvrez **Hypon Local Bridge** → **Installer** → onglet
**Configuration**, renseignez l'IP dans `inverter_ip`, puis démarrez l'add-on et
surveillez l'onglet **Journaux**. Un appareil *Hypontech* avec ses capteurs apparaît
ensuite dans **Paramètres → Appareils et services → MQTT**.

**Ça ne marche pas ?** En plein jour, `does not answer ARP` dans les journaux indique
une mauvaise IP (refaites l'étape 2) ou un autre réseau/VLAN. Plus d'aide dans le
[`DOCS.fr.md`](hypon-local-addon/DOCS.fr.md) de l'add-on.

## Principe

Le module envoie ses mesures en MQTT clair (TCP 1883) vers `hk.hypon.cloud`.
L'add-on, sur le réseau de l'hôte HA :

1. empoisonne l'ARP du **seul** micro-onduleur (il croit que la passerelle = HA) ;
2. redirige son port 1883 vers un pont local via `iptables`, et réachemine le reste ;
3. relaie vers le vrai cloud + décode + publie les capteurs dans HA ;
4. restaure tout proprement à l'arrêt.

Il est en Python pur (bibliothèque standard uniquement) et reste à ~0 % de CPU au
repos. Voir le [`DOCS.fr.md`](hypon-local-addon/DOCS.fr.md) pour toutes les options et
le dépannage.

## Statut

Testée sur une installation HA réelle.

## Licence

[MIT](LICENSE)
