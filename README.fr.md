# LocalHyponTech — dépôt d'add-ons Home Assistant

[English](README.md) · **Français**

Dépôt d'add-on personnalisé pour Home Assistant contenant **Hypon Local Bridge** :
lecture **locale** des données d'un micro-onduleur Hypon (collecteur `COM-WF`,
`HMS-800W` et variantes), **sans toucher au DNS ni à la box**, avec publication
automatique dans Home Assistant (MQTT Discovery) et relais transparent vers le cloud
Hypon (l'appli constructeur continue de fonctionner).

Version « légère » : pas de bettercap. L'ARP ciblé et la redirection sont faits en
Python pur (bibliothèque standard) + une règle `iptables`.

## Principe

Le module envoie ses mesures en MQTT clair (TCP 1883) vers `hk.hypon.cloud`.
L'add-on, sur le réseau de l'hôte HA :

1. empoisonne l'ARP du **seul** micro-onduleur (il croit que la passerelle = HA) ;
2. redirige son port 1883 vers un pont local via `iptables`, et réachemine le reste ;
3. relaie vers le vrai cloud + décode + publie les capteurs dans HA ;
4. restaure tout proprement à l'arrêt.

Voir le [`DOCS.fr.md`](hypon-local-addon/DOCS.fr.md) de l'add-on pour les options et
le dépannage.

## Ajouter ce dépôt à Home Assistant

**Paramètres → Modules complémentaires → Boutique → menu ⋮ → Dépôts**, puis ajoute :

```
https://github.com/Pyozer/ha-local-hypontech
```

Installe ensuite **Hypon Local Bridge**, renseigne l'IP du micro-onduleur, démarre.

## Prérequis

- Home Assistant OS / Supervised.
- HA et le micro-onduleur sur le **même segment L2** (même sous-réseau, sans VLAN
  ni routeur entre les deux).
- Un broker MQTT (add-on Mosquitto) — identifiants récupérés automatiquement.

## Structure du dépôt

```
.
├── repository.yaml
└── hypon-local-addon/
    ├── config.yaml
    ├── Dockerfile
    ├── run.sh
    ├── DOCS.md / DOCS.fr.md
    └── app/
        ├── hypon_intercept.py   # ARP ciblé + iptables + cycle de vie
        └── hypon_bridge.py      # pont MQTT (relais cloud + décodage + Discovery)
```

## Statut

Première version, **non encore testée sur une installation HA réelle**.
