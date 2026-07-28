ARG BUILD_FROM
FROM $BUILD_FROM

RUN pip3 install --no-cache-dir appdaemon==4.*

COPY rootfs /
RUN chmod a+x /etc/services.d/gridlock/run /etc/services.d/gridlock/finish

WORKDIR /opt/appdaemon
