# Hypon Local Bridge — add-on Home Assistant

Lecture **locale** des données d'un micro-onduleur Hypon (collecteur `COM-WF`,
`HMS-800W` et variantes), **sans toucher au DNS ni à la box**, avec publication
automatique dans Home Assistant (MQTT Discovery) et relais transparent vers le cloud
Hypon pour garder l'application constructeur fonctionnelle.

Version « légère » : pas de bettercap. L'ARP ciblé et la redirection sont faits en
Python pur (bibliothèque standard) + une règle `iptables`.

## Principe

Le module envoie ses mesures en MQTT clair (TCP 1883) vers `hk.hypon.cloud`.
L'add-on, sur le réseau de l'hôte HA :

1. empoisonne l'ARP du **seul** micro-onduleur (il croit que la passerelle = HA) ;
2. redirige son port 1883 vers un pont local via `iptables`, et réachemine le reste ;
3. relaie vers le vrai cloud + décode + publie les capteurs dans HA ;
4. restaure tout proprement à l'arrêt.

Voir [`DOCS.md`](DOCS.md) pour les options et le dépannage.

## Contenu

```
hypon-local-addon/
├── config.yaml          # métadonnées + options de l'add-on
├── Dockerfile           # image (base HA + python3 + iptables)
├── run.sh               # lanceur bashio : config -> variables d'env
├── DOCS.md              # documentation affichée dans HA
└── app/
    ├── hypon_intercept.py   # ARP ciblé + iptables + cycle de vie
    └── hypon_bridge.py      # pont MQTT (relais cloud + décodage + Discovery)
```

## Déploiement en tant que dépôt d'add-on personnalisé

À cause des droits réseau requis (`host_network`, `NET_ADMIN`/`NET_RAW`), cet add-on
ne passe pas par le store officiel. On le distribue via un dépôt Git :

```
mon-depot/
├── repository.yaml
└── hypon-local-addon/      <-- ce dossier
```

`repository.yaml` à la racine du dépôt :

```yaml
name: Mes add-ons Hypon
url: https://github.com/votre-compte/mon-depot
maintainer: Votre Nom <vous@example.com>
```

Puis dans HA : **Paramètres → Modules complémentaires → Boutique → ⋮ → Dépôts**,
ajoutez l'URL du dépôt, installez *Hypon Local Bridge*, configurez `inverter_ip`,
démarrez.

## Prérequis

- Home Assistant OS / Supervised.
- HA et le micro-onduleur sur le **même segment L2** (même sous-réseau, sans VLAN
  ni routeur entre les deux).
- Un broker MQTT (add-on Mosquitto). Les identifiants sont récupérés automatiquement.

## Statut

Code complet, **non encore testé sur une installation HA réelle**. À valider :
le socket raw ARP et `iptables` sous le conteneur HA OS (droits `NET_RAW`/`NET_ADMIN`),
et la résolution du broker MQTT en `host_network`.
