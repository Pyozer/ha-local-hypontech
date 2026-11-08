ARG BUILD_FROM
FROM ${BUILD_FROM}

# python3 pour le pont/intercepteur, iptables pour la redirection ciblée.
# (le reste — socket raw ARP, /proc — ne demande aucun paquet supplémentaire)
RUN apk add --no-cache python3 iptables

COPY app/ /app/
COPY run.sh /
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
