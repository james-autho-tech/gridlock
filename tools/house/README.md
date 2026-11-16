# House picture

The Overview's power-flow picture is a 3D render made by `render_house.py`, with the live wires, pulses and figures
drawn over it in the dashboard.

    blender -b -P tools/house/render_house.py -- /tmp/house 192
    magick /tmp/house/house.png -quality 86 -define webp:alpha-quality=90 gridlock_addon/rootfs/opt/gridlock-web/assets/house.webp
    cp /tmp/house/house.json gridlock_addon/rootfs/opt/gridlock-web/assets/house.json

Needs Blender 4.2 LTS (rendered on CPU with Cycles, about 20 s on 32 cores). `house.json` holds every wire route and
label anchor projected onto the picture in pixels, so after changing the scene always copy both files together.
