# -*- coding: utf-8 -*-
"""拼 bpy 子进程脚本字符串: 渲染预览、glb/stl 导出(复用已验证写法)。
这些脚本运行在 Blender 内部(bpy 可用); 由 blender_runner 落临时文件执行。"""
import math


def _quoted(p):
    return repr(str(p))  # 生成合法 python 字面量(处理 Windows 反斜杠)


def render_preview_code(blend_in, out_png, view="iso", res=(720, 540)):
    rx, ry = int(res[0]), int(res[1])
    return f"""import bpy, mathutils, os, math
bpy.ops.wm.open_mainfile(filepath={_quoted(blend_in)})
sc = bpy.context.scene
try:
    sc.render.engine = 'BLENDER_EEVEE_NEXT'
except Exception:
    sc.render.engine = 'BLENDER_EEVEE'
sc.render.image_settings.file_format = 'PNG'
sc.render.resolution_x = {rx}
sc.render.resolution_y = {ry}
try:
    sc.view_settings.view_transform = 'Standard'  # 别用 AgX/Filmic, 会压暗黄/绿, 评审失真
except Exception:
    pass
sc.render.film_transparent = False
meshes = [o for o in bpy.data.objects if o.type == 'MESH']
INF = 1e9
mins = mathutils.Vector((INF, INF, INF)); maxs = mathutils.Vector((-INF, -INF, -INF))
for o in meshes:
    for v in o.bound_box:
        p = o.matrix_world @ mathutils.Vector(v)
        for i in range(3):
            mins[i] = min(mins[i], p[i]); maxs[i] = max(maxs[i], p[i])
center = (mins + maxs) / 2.0
diag = (maxs - mins).length
if diag < 1e-6:
    diag = 1.0
for o in list(bpy.data.objects):
    if o.type in ('CAMERA', 'LIGHT'):
        bpy.data.objects.remove(o, do_unlink=True)
R = diag * 1.7
view = {_quoted(view)}
if view == 'front':
    cam_pos = center + mathutils.Vector((0.0, R, R * 0.12))     # +Y: 看模型"前脸"(约定前脸朝 +Y)
elif view == 'side':
    cam_pos = center + mathutils.Vector((R, 0.0, R * 0.12))
elif view == 'top':
    cam_pos = center + mathutils.Vector((0.0, 0.0, R))
else:
    cam_pos = center + mathutils.Vector((R * 0.9, R * 1.15, R * 0.8))  # iso 从 +X+Y 看前脸
def look_at(cam, target):
    d = target - cam.location
    cam.rotation_euler = d.to_track_quat('-Z', 'Y').to_euler()
cam_ob = bpy.data.objects.new('PreviewCam', bpy.data.cameras.new('PreviewCam'))
sc.collection.objects.link(cam_ob)
cam_ob.location = cam_pos
look_at(cam_ob, center)
sc.camera = cam_ob
def _add_sun(name, direction, energy, color):
    bpy.ops.object.light_add(type='SUN', location=(0, 0, 0))
    s = bpy.context.object
    s.name = name
    s.data.energy = energy
    s.data.color = color
    q = mathutils.Vector((0, 0, -1)).rotation_difference(mathutils.Vector(direction).normalized())
    s.rotation_euler = q.to_euler()
# 主光打在前脸(+Y)/顶, 让相机能拍清五官; 背景光补 -Y 轮廓
_add_sun('SunMain', (0.45, 0.9, 0.55), 4.0, (1.0, 0.97, 0.9))
_add_sun('SunFill', (0.9, 0.1, -0.15), 1.5, (0.88, 0.92, 1.0))
_add_sun('SunCool', (-0.35, -1.0, 0.3), 1.2, (0.9, 0.94, 1.0))
if not bpy.data.worlds:
    w = bpy.data.worlds.new('World'); sc.world = w
else:
    w = bpy.data.worlds[0]; sc.world = w
w.use_nodes = True
bg = w.node_tree.nodes.get('Background')
if bg is not None:
    bg.inputs[0].default_value = (0.48, 0.67, 0.93, 1.0)  # 淡蓝天空(线性)
    bg.inputs[1].default_value = 1.0
sc.render.filepath = {_quoted(out_png)}
bpy.ops.render.render(write_still=True)
print('RENDER_DONE', flush=True)
"""


def export_glb_stl_code(blend_in, glb_out=None, stl_out=None):
    parts = [f"import bpy, os, struct, mathutils\n"
             f"bpy.ops.wm.open_mainfile(filepath={_quoted(blend_in)})\n"]
    if glb_out is not None:
        parts.append(
            "try:\n"
            "    bpy.ops.preferences.addon_enable(module='io_scene_gltf2')\n"
            "except Exception as e:\n"
            "    print('gltf_addon_err', e)\n"
            f"bpy.ops.export_scene.gltf(filepath={_quoted(glb_out)}, export_format='GLB')\n")
    if stl_out is not None:
        parts.append(f"""S = mathutils.Matrix.Scale(1000.0, 4)
objs = [o for o in bpy.data.objects if o.type == 'MESH']
tris = []
for ob in objs:
    md = ob.data
    md.calc_loop_triangles()
    for lt in md.loop_triangles:
        co = [md.vertices[i].co for i in lt.vertices]
        p = [S @ (ob.matrix_world @ v) for v in co]
        u = p[1] - p[0]; w = p[2] - p[0]
        n = u.cross(w)
        if n.length < 1e-9:
            continue
        n = n / n.length
        tris.append((n, p[0], p[1], p[2]))
with open({_quoted(stl_out)}, 'wb') as f:
    f.write(b'blender agent export'[:80].ljust(80, b'\\0'))
    f.write(struct.pack('<I', len(tris)))
    for n, a, b, c in tris:
        f.write(struct.pack('<12f', n.x, n.y, n.z, a.x, a.y, a.z, b.x, b.y, b.z, c.x, c.y, c.z))
        f.write(struct.pack('<H', 0))
print('STL_TRIANGLES', len(tris), flush=True)
""")
    parts.append("print('EXPORT_DONE', flush=True)\n")
    return "".join(parts)
