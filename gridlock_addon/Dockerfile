ARG BUILD_FROM
FROM $BUILD_FROM

# glpk gives the LP optimiser (core/optimizer.py) a musl-native solver
# binary — PuLP's own bundled CBC is glibc-linked and doesn't run on
# this Alpine base image; optimizer.py detects glpsol via shutil.which
# and prefers it, falling back to PuLP's bundled solver only where
# glpsol isn't found (e.g. local dev on a glibc machine).
# Pinned below 4.0: PuLP 4.0 removes the direct LpVariable(name, low, up)
# construction core/optimizer.py uses (deprecated as of 3.x) in favour of
# prob.add_variable(...) — an unpinned upgrade would break the solve
# silently on a future rebuild rather than at review time.
RUN apk add --no-cache glpk
RUN pip3 install --no-cache-dir appdaemon==4.* pyyaml "pulp>=2.7,<4"

COPY rootfs /
RUN chmod a+x \
    /etc/services.d/gridlock/run /etc/services.d/gridlock/finish \
    /etc/services.d/gridlock-web/run /etc/services.d/gridlock-web/finish

WORKDIR /opt/appdaemon
