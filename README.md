# LocalHyponTech — dépôt d'add-ons Home Assistant

Dépôt d'add-on personnalisé pour Home Assistant contenant **Hypon Local Bridge** :
lecture **locale** des données d'un micro-onduleur Hypon (collecteur `COM-WF`,
`HMS-800W` et variantes), **sans toucher au DNS ni à la box**, avec publication
automatique dans Home Assistant (MQTT Discovery) et relais transparent vers le cloud
Hypon (l'appli constructeur continue de fonctionner).

## Ajouter ce dépôt à Home Assistant

**Paramètres → Modules complémentaires → Boutique → menu ⋮ → Dépôts**, puis ajoute :

```
https://github.com/Pyozer/ha-local-hypontech
```

Installe ensuite **Hypon Local Bridge**, renseigne l'IP du micro-onduleur, démarre.

> Dépôt privé : Home Assistant doit pouvoir le cloner. Rends-le public, ou fournis
> un token dans l'URL (`https://TOKEN@github.com/Pyozer/ha-local-hypontech`).

## Add-on

- [`hypon-local-addon/`](hypon-local-addon/) — l'add-on (voir son
  [`DOCS.md`](hypon-local-addon/DOCS.md) pour les options et le dépannage).

## Prérequis

- Home Assistant OS / Supervised.
- HA et le micro-onduleur sur le **même segment L2** (même sous-réseau, sans VLAN
  ni routeur entre les deux).
- Un broker MQTT (add-on Mosquitto) — identifiants récupérés automatiquement.

## Statut

Première version, **non encore testée sur une installation HA réelle**.
