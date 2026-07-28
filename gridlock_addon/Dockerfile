ARG BUILD_FROM
FROM $BUILD_FROM

RUN pip3 install --no-cache-dir appdaemon==4.* pyyaml

COPY rootfs /
RUN chmod a+x \
    /etc/services.d/gridlock/run /etc/services.d/gridlock/finish \
    /etc/services.d/gridlock-web/run /etc/services.d/gridlock-web/finish

WORKDIR /opt/appdaemon
