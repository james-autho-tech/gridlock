"""Render the dashboard's house picture, and where its wires and labels go.

Run with Blender 4.2 LTS (headless):

    blender -b -P tools/house/render_house.py -- <out_dir> [samples]

Writes <out_dir>/house.png (transparent background, the house standing on a
shadow) and <out_dir>/house.json: the image size, and every wire route and
label anchor projected onto the image in pixels, so the dashboard can draw
the live wires and pulses exactly over the walls. The scene is built here
from simple shapes: change it, re-render, copy both files into
gridlock_addon/rootfs/opt/gridlock-web/assets/.

Coordinates in metres: x along the front (left to right), y back from the
front wall, z up. Garage on the left, house on the right, grid pole beyond.
"""
import json
import math
import os
import sys

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
OUT = argv[0] if argv else "/tmp/house"
SAMPLES = int(argv[1]) if len(argv) > 1 else 160
W, H = 1600, 1000
os.makedirs(OUT, exist_ok=True)

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene


# ---- materials -------------------------------------------------------------
def mat(name, color, rough=0.6, metal=0.0, emit=None, strength=0.0, alpha=1.0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*color, 1.0)
    b.inputs["Roughness"].default_value = rough
    b.inputs["Metallic"].default_value = metal
    if emit:
        b.inputs["Emission Color"].default_value = (*emit, 1.0)
        b.inputs["Emission Strength"].default_value = strength
    return m


M = {
    "wall": mat("wall", (0.20, 0.21, 0.23), 0.85),
    "wall_dark": mat("wall_dark", (0.12, 0.13, 0.15), 0.85),
    "roof": mat("roof", (0.035, 0.038, 0.045), 0.55),
    "trim": mat("trim", (0.06, 0.065, 0.075), 0.5),
    "panel": mat("panel", (0.02, 0.035, 0.08), 0.18, 0.3),
    "panel_frame": mat("panel_frame", (0.35, 0.37, 0.40), 0.35, 0.8),
    "window": mat("window", (0.9, 0.7, 0.4), 0.3, emit=(1.0, 0.72, 0.42), strength=2.2),
    "glass_dark": mat("glass_dark", (0.02, 0.025, 0.03), 0.1),
    "door": mat("door", (0.10, 0.075, 0.055), 0.6),
    "white": mat("white", (0.82, 0.84, 0.87), 0.35),
    "garage_in": mat("garage_in", (0.16, 0.16, 0.17), 0.9),
    "floor": mat("floor", (0.11, 0.11, 0.115), 0.8),
    "drive": mat("drive", (0.075, 0.078, 0.085), 0.9),
    "car": mat("car", (0.03, 0.09, 0.30), 0.18, 0.7),
    "car_glass": mat("car_glass", (0.01, 0.012, 0.015), 0.05, 0.2),
    "tyre": mat("tyre", (0.015, 0.015, 0.017), 0.8),
    "tail": mat("tail", (0.6, 0.02, 0.02), 0.3, emit=(1.0, 0.05, 0.03), strength=1.2),
    "fan": mat("fan", (0.05, 0.055, 0.06), 0.5),
    "pole": mat("pole", (0.16, 0.13, 0.10), 0.85),
}


def obj_from(name, verts, faces, material, bevel=0.0):
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    me.update()
    o = bpy.data.objects.new(name, me)
    scene.collection.objects.link(o)
    o.data.materials.append(material)
    if bevel:
        mod = o.modifiers.new("bevel", "BEVEL")
        mod.width, mod.segments = bevel, 3
    return o


def box(name, x0, x1, y0, y1, z0, z1, material, bevel=0.0):
    v = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    f = [(0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1), (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0)]
    return obj_from(name, v, f, material, bevel)


def prism_x(name, x0, x1, profile, material):
    """A shape extruded along x from a (y, z) outline (anticlockwise)."""
    n = len(profile)
    v = [(x0, y, z) for y, z in profile] + [(x1, y, z) for y, z in profile]
    f = [tuple(range(n))[::-1], tuple(range(n, 2 * n))] + [(i, (i + 1) % n, n + (i + 1) % n, n + i) for i in range(n)]
    return obj_from(name, v, f, material)


def prism_y(name, y0, y1, profile, material):
    """A shape extruded along y from an (x, z) outline."""
    n = len(profile)
    v = [(x, y0, z) for x, z in profile] + [(x, y1, z) for x, z in profile]
    f = [tuple(range(n)), tuple(range(n, 2 * n))[::-1]] + [(i, n + i, n + (i + 1) % n, (i + 1) % n) for i in range(n)]
    return obj_from(name, v, f, material)


def slab(name, a, b, c, d, thick, material):
    """A thin slab over the quad a-b-c-d, thickened along its normal."""
    a, b, c, d = map(Vector, (a, b, c, d))
    nrm = (b - a).cross(d - a).normalized() * thick
    v = [tuple(p) for p in (a, b, c, d)] + [tuple(p + nrm) for p in (a, b, c, d)]
    f = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    return obj_from(name, v, f, material)


# ---- the house (x 0..8, y 0..6, two storeys) -------------------------------
HW, HD, HZ, RIDGE = 8.0, 6.0, 5.2, 7.5
box("house", 0, HW, 0, HD, 0, HZ, M["wall"])
prism_x("gables", 0, HW, [(0, HZ), (HD, HZ), (HD / 2, RIDGE - 0.15)], M["wall"])
OV = 0.4      # roof overhang
front_a, front_b = Vector((-0.3, -OV, HZ - 0.3)), Vector((HW + 0.3, -OV, HZ - 0.3))
ridge_a, ridge_b = Vector((-0.3, HD / 2, RIDGE)), Vector((HW + 0.3, HD / 2, RIDGE))
back_a, back_b = Vector((-0.3, HD + OV, HZ - 0.3)), Vector((HW + 0.3, HD + OV, HZ - 0.3))
slab("roof_front", front_a, front_b, ridge_b, ridge_a, 0.16, M["roof"])
slab("roof_back", ridge_a, ridge_b, back_b, back_a, 0.16, M["roof"])


def on_front_slope(x, t):
    """A point on the front roof slope: x along it, t from eave (0) to ridge (1)."""
    p = front_a.lerp(ridge_a, t)
    return Vector((x, p.y, p.z))


slope_n = (front_b - front_a).cross(ridge_a - front_a).normalized()
for r in range(2):
    for c in range(5):
        x0 = 0.75 + c * 1.33
        t0 = 0.16 + r * 0.38
        a = on_front_slope(x0, t0) + slope_n * 0.2
        b = on_front_slope(x0 + 1.22, t0) + slope_n * 0.2
        cc = on_front_slope(x0 + 1.22, t0 + 0.35) + slope_n * 0.2
        d = on_front_slope(x0, t0 + 0.35) + slope_n * 0.2
        slab(f"panel_{r}_{c}", a, b, cc, d, 0.04, M["panel"])
        slab(f"panel_frame_{r}_{c}", a - slope_n * 0.02, b - slope_n * 0.02, cc - slope_n * 0.02, d - slope_n * 0.02, 0.025, M["panel_frame"])


def window(name, x0, x1, z0, z1, lit=True):
    box(name + "_frame", x0 - 0.08, x1 + 0.08, -0.06, 0.0, z0 - 0.08, z1 + 0.08, M["trim"])
    box(name, x0, x1, -0.08, -0.02, z0, z1, M["window"] if lit else M["glass_dark"])
    xm = (x0 + x1) / 2
    box(name + "_bar", xm - 0.03, xm + 0.03, -0.1, -0.05, z0, z1, M["trim"])


window("win_ground", 2.7, 5.1, 0.9, 2.3)
window("win_up_l", 1.1, 2.6, 3.2, 4.5)
window("win_up_r", 4.9, 6.7, 3.2, 4.5, lit=False)
box("front_door", 5.75, 6.7, -0.06, 0.0, 0.0, 2.2, M["door"])
box("door_step", 5.6, 6.85, -0.5, 0.0, 0.0, 0.08, M["trim"])

# Battery on the front wall by the garage.
box("battery", 0.55, 1.5, -0.24, -0.02, 0.35, 2.15, M["white"], bevel=0.05)
# Electricity meter by the front door.
box("meter", 7.15, 7.6, -0.14, -0.02, 1.45, 2.05, M["white"], bevel=0.02)
# Heat pump on the ground in front, right of the door.
box("heatpump", 6.95, 7.95, -0.75, -0.25, 0.0, 0.85, M["white"], bevel=0.03)
bpy.ops.mesh.primitive_cylinder_add(radius=0.27, depth=0.04, location=(7.33, -0.77, 0.43), rotation=(math.pi / 2, 0, 0))
bpy.context.object.data.materials.append(M["fan"])

# ---- the garage (x -4.4..0), door open, the car parked nose-in -------------
GX0, GX1, GZ, GR = -4.4, 0.0, 3.0, 4.4
# A shell, not a solid: outer side wall and the back gable wall (the house's
# own wall closes the right side), so the open door shows the inside.
box("garage_side", GX0, GX0 + 0.2, 0.3, HD, 0.0, GZ, M["wall_dark"])
prism_y("garage_back", HD - 0.2, HD, [(GX0, 0), (GX1, 0), (GX1, GZ), ((GX0 + GX1) / 2, GR), (GX0, GZ)], M["wall_dark"])
box("garage_floor", GX0 + 0.1, GX1 - 0.1, 0.0, HD - 0.1, 0.0, 0.02, M["floor"])
box("garage_inside_back", GX0 + 0.15, GX1 - 0.15, HD - 0.4, HD - 0.3, 0.0, GZ, M["garage_in"])
box("garage_inside_l", GX0 + 0.15, GX0 + 0.25, 0.3, HD - 0.3, 0.0, GZ, M["garage_in"])
box("garage_inside_r", GX1 - 0.25, GX1 - 0.15, 0.3, HD - 0.3, 0.0, GZ, M["garage_in"])
box("garage_ceiling", GX0 + 0.15, GX1 - 0.15, 0.3, HD - 0.3, GZ - 0.12, GZ - 0.02, M["garage_in"])
DX0, DX1, DZ = -3.85, -0.55, 2.45
prism_y("garage_front", 0.0, 0.3, [(GX0, 0), (DX0, 0), (DX0, DZ), (DX1, DZ), (DX1, 0), (GX1, 0), (GX1, GZ),
                                    ((GX0 + GX1) / 2, GR), (GX0, GZ)], M["wall"])
gp = lambda x, z: (x, z)
slab("garage_roof_l", (GX0 - 0.3, -OV, GZ - 0.2), (GX0 - 0.3, HD + 0.1, GZ - 0.2),
     ((GX0 + GX1) / 2, HD + 0.1, GR + 0.05), ((GX0 + GX1) / 2, -OV, GR + 0.05), 0.15, M["roof"])
slab("garage_roof_r", ((GX0 + GX1) / 2, -OV, GR + 0.05), ((GX0 + GX1) / 2, HD + 0.1, GR + 0.05),
     (GX1 + 0.3, HD + 0.1, GZ - 0.2), (GX1 + 0.3, -OV, GZ - 0.2), 0.15, M["roof"])
box("door_header_trim", DX0 - 0.08, DX1 + 0.08, -0.04, 0.0, DZ, DZ + 0.1, M["trim"])
box("charger", -0.5, -0.38, 0.6, 0.9, 1.0, 1.45, M["white"], bevel=0.02)

# The car, nose-in: rear at y 0.9.
CX, CY0, CY1 = (DX0 + DX1) / 2, 0.9, 5.4
box("car_body", CX - 0.93, CX + 0.93, CY0, CY1, 0.32, 1.02, M["car"], bevel=0.18)
cabin = box("car_cabin", CX - 0.78, CX + 0.78, CY0 + 1.0, CY1 - 1.4, 0.95, 1.5, M["car_glass"], bevel=0.14)
for vtx in cabin.data.vertices:   # taper the cabin
    if vtx.co.z > 1.2:
        vtx.co.x = CX + (vtx.co.x - CX) * 0.82
box("car_roof", CX - 0.6, CX + 0.6, CY0 + 1.5, CY1 - 1.9, 1.48, 1.55, M["car"], bevel=0.05)
box("tail_light", CX - 0.8, CX + 0.8, CY0 - 0.02, CY0 + 0.03, 0.84, 0.875, M["tail"])
for wx in (CX - 0.9, CX + 0.9):
    for wy in (CY0 + 0.85, CY1 - 0.85):
        bpy.ops.mesh.primitive_cylinder_add(radius=0.36, depth=0.26, location=(wx, wy, 0.36), rotation=(0, math.pi / 2, 0))
        bpy.context.object.data.materials.append(M["tyre"])

# ---- ground: driveway, path, and a shadow catcher ---------------------------
box("driveway", GX0 + 0.1, GX1 - 0.1, -6.5, 0.0, 0.0, 0.015, M["drive"])
box("path", 5.6, 6.85, -6.5, -0.5, 0.0, 0.012, M["drive"])
# No shadow catcher: on the dashboard's dark background its shadows read as
# boxy dark patches. The dashboard fades the bottom of the picture instead.

# ---- the grid pole ------------------------------------------------------------
POLE = Vector((11.2, -2.0, 0))
bpy.ops.mesh.primitive_cylinder_add(radius=0.12, depth=8.0, location=(POLE.x, POLE.y, 4.0))
bpy.context.object.data.materials.append(M["pole"])
box("crossarm", POLE.x - 0.9, POLE.x + 0.9, POLE.y - 0.06, POLE.y + 0.06, 7.45, 7.6, M["pole"])

# ---- light and camera ------------------------------------------------------------
world = bpy.data.worlds.new("night")
scene.world = world
world.use_nodes = True
world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.012, 0.016, 0.03, 1)
world.node_tree.nodes["Background"].inputs["Strength"].default_value = 1.0


def light(kind, loc, energy, color=(1, 1, 1), size=1.0, rot=None):
    d = bpy.data.lights.new(kind, kind)
    d.energy, d.color = energy, color
    if kind == "AREA":
        d.size = size
    o = bpy.data.objects.new(kind, d)
    o.location = loc
    if rot:
        o.rotation_euler = rot
    scene.collection.objects.link(o)
    return o


moon = light("SUN", (0, 0, 20), 0.35, (0.6, 0.7, 1.0), rot=(math.radians(55), 0, math.radians(-35)))
light("AREA", (-6, -12, 10), 900, (0.75, 0.82, 1.0), size=10, rot=(math.radians(55), 0, math.radians(-25)))
light("POINT", (CX, 3.2, GZ - 0.4), 250, (1.0, 0.85, 0.65))                    # garage ceiling light
light("POINT", (6.2, -0.9, 2.6), 60, (1.0, 0.8, 0.55))                         # porch light
light("AREA", (3.9, -1.5, 1.6), 120, (1.0, 0.75, 0.45), size=2.4, rot=(math.radians(90), 0, 0))  # window spill

cam_d = bpy.data.cameras.new("cam")
cam_d.lens = 38
cam = bpy.data.objects.new("cam", cam_d)
scene.collection.objects.link(cam)
scene.camera = cam
cam.location = (-12.0, -19.5, 10.5)
target = Vector((3.2, 2.4, 2.0))
cam.rotation_euler = (target - cam.location).to_track_quat("-Z", "Y").to_euler()

scene.render.engine = "CYCLES"
scene.cycles.device = "CPU"
scene.cycles.samples = SAMPLES
scene.cycles.use_denoising = True
scene.render.resolution_x, scene.render.resolution_y = W, H
scene.render.film_transparent = True
scene.view_settings.view_transform = "AgX"
scene.view_settings.look = "AgX - Medium High Contrast"
scene.render.image_settings.file_format = "PNG"
scene.render.image_settings.color_mode = "RGBA"
scene.render.filepath = os.path.join(OUT, "house.png")

# ---- where the wires and labels go, projected to pixels ----------------------
F = -0.03   # just in front of the front wall
bpy.context.view_layer.update()   # the camera's matrix, before projecting through it


def px(p):
    v = world_to_camera_view(scene, cam, Vector(p))
    return [round(v.x * W, 1), round((1 - v.y) * H, 1)]


def sag(a, b, drop, n=12):
    a, b = Vector(a), Vector(b)
    return [tuple(a.lerp(b, i / n) - Vector((0, 0, drop * 4 * (i / n) * (1 - i / n)))) for i in range(n + 1)]


eave_x = 0.35
wires = {
    # Panels -> down the house's left corner (clear of the windows) -> top of the battery.
    "solar": [tuple(on_front_slope(0.9, 0.16) + slope_n * 0.22), (eave_x, -OV, HZ - 0.3), (eave_x, F, HZ - 0.4),
              (eave_x, F, 2.45), (1.0, F, 2.45), (1.0, F, 2.15)],
    # Pole -> meter (a sagging drop cable), then under the upstairs windows to the battery.
    "grid": sag((POLE.x, POLE.y, 7.5), (7.6, F, 2.0), 0.6) + [(7.38, F, 2.75), (1.3, F, 2.75), (1.3, F, 2.15)],
    # Battery -> into the house beside the downstairs window.
    "home": [(1.5, F, 1.5), (2.55, F, 1.5)],
    # Battery -> charger inside the garage -> the car's charge port.
    "car": [(0.55, F, 1.2), (-0.2, F, 1.2), (-0.44, 0.6, 1.2), (CX + 0.95, CY0 + 0.25, 0.85)],
    # Battery -> along the base of the wall -> the heat pump.
    "heatpump": [(1.0, F, 0.35), (1.0, F, 0.1), (6.95, F, 0.1), (7.1, -0.25, 0.3)],
}
anchors = {
    "grid": (POLE.x, POLE.y, 7.6),
    "heatpump": (7.45, -0.5, 0.86),
    "home": wires["home"][-1],        # where the house's supply goes in
    "solar": tuple(on_front_slope(3.9, 0.55) + slope_n * 0.22),
    "battery": (1.02, -0.24, 2.15),
    "car": (CX, CY0 + 0.35, 1.08),
}
# Where each figure goes: a point in the scene beside its thing, so a
# re-render keeps them in place. Labels above, beside or below their thing.
labels = {
    "solar": (4.2, 3.0, RIDGE + 2.4),
    "home": (4.3, -4.4, 0.0),
    "grid": (POLE.x, POLE.y, 9.8),
    "car": (GX0 - 3.6, -1.2, 1.1),
    "battery": (0.2, -5.0, 0.0),
    "heatpump": (9.4, -3.6, 0.0),
}
data = {"width": W, "height": H,
        "wires": {k: [px(p) for p in pts] for k, pts in wires.items()},
        "anchors": {k: px(p) for k, p in anchors.items()},
        "labels": {k: px(p) for k, p in labels.items()}}

bpy.ops.render.render(write_still=True)
print("rendered", scene.render.filepath)

# The picture's visible extent, so the dashboard can crop to it.
img = bpy.data.images.load(scene.render.filepath)
px_ = list(img.pixels)
xs, ys = [], []
for yy in range(0, H, 4):
    row = (H - 1 - yy) * W * 4
    for xx in range(0, W, 4):
        if px_[row + xx * 4 + 3] > 0.04:
            xs.append(xx)
            ys.append(yy)
data["bbox"] = [min(xs), min(ys), max(xs), max(ys)] if xs else [0, 0, W, H]
with open(os.path.join(OUT, "house.json"), "w") as f:
    json.dump(data, f, indent=1)
