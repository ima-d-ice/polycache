#!/usr/bin/env python3
"""Render demo.gif from a real PolyCache session.

The commands below are actually executed against a freshly started server;
their stdout/stderr is what appears in the GIF. The only synthetic part is the
terminal chrome and the typing reveal.

Requires Pillow (`python3 -m pip install pillow`) and redis-cli.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.request

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 6399
ADMIN = 8099
WIDTH, HEIGHT = 1200, 800
TOP = 78
LINE_HEIGHT = 23
FONT_SIZE = 17
WRAP = 112

BG = (13, 17, 23)
BAR = (22, 27, 34)
BORDER = (48, 54, 61)
TEXT = (230, 237, 243)
DIM = (139, 148, 158)
GREEN = (63, 185, 80)
BLUE = (88, 166, 255)
RED = (248, 81, 73)


def font(size=FONT_SIZE):
    for path in ("/System/Library/Fonts/Menlo.ttc",
                 "/System/Library/Fonts/SFNSMono.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


MONO = font()
TITLE_FONT = font(15)


def wait_ready(proc):
    deadline = time.time() + 10
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit("server exited during startup")
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/metrics" % ADMIN, timeout=0.5) as response:
                json.loads(response.read())
                return
        except Exception:
            time.sleep(0.05)
    raise SystemExit("server did not become ready on port %d" % PORT)


def run(command, stdin=None, cwd=None):
    result = subprocess.run(command, shell=isinstance(command, str),
                            input=stdin, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, cwd=cwd)
    return result.returncode, result.stdout.decode("utf-8", "replace").strip()


def main():
    tmpdir = tempfile.mkdtemp(prefix="polycache_demo_")
    image_path = os.path.join(tmpdir, "demo.jpg")
    out_path = os.path.join(tmpdir, "out.jpg")

    # A noise image compresses poorly, so it lands above the old 64KB cap.
    from PIL import Image as PILImage
    pixels = os.urandom(800 * 600 * 3)
    PILImage.frombytes("RGB", (800, 600), pixels).save(image_path, quality=88)
    image_size = os.path.getsize(image_path)
    image_bytes = open(image_path, "rb").read()

    server_cmd = [os.path.join(ROOT, "polycache"),
                  "--port", str(PORT),
                  "--admin-port", str(ADMIN),
                  "--memory-limit", "1024",
                  "--max-value-size", "512",
                  "--no-aof"]
    proc = subprocess.Popen(server_cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    try:
        wait_ready(proc)
        startup = proc.stdout.readline().strip()

        events = []

        def command(text):
            events.append(("cmd", "$ " + text))

        def output(text, color="out"):
            text = text.strip()
            if not text:
                return
            for line in text.splitlines():
                events.append((color, line))

        command(" ".join(["./polycache"] + server_cmd[1:]))
        output(startup, "dim")

        command('redis-cli -p %d SET greeting "hello polycache"' % PORT)
        output(run(["redis-cli", "-p", str(PORT), "SET", "greeting",
                    "hello polycache"])[1], "ok")
        command("redis-cli -p %d GET greeting" % PORT)
        output(run(["redis-cli", "-p", str(PORT), "GET", "greeting"])[1])

        command('redis-cli -p %d SET row:42 \'{"id":42,"name":"alice","active":true}\'' % PORT)
        output(run(["redis-cli", "-p", str(PORT), "SET", "row:42",
                    '{"id":42,"name":"alice","active":true}'])[1], "ok")
        command("redis-cli -p %d GET row:42" % PORT)
        output(run(["redis-cli", "-p", str(PORT), "GET", "row:42"])[1])

        command("redis-cli -p %d -x SET image:demo < demo.jpg" % PORT)
        output(run(["redis-cli", "-p", str(PORT), "-x", "SET", "image:demo"],
                   stdin=image_bytes)[1], "ok")

        command("redis-cli -p %d --raw GET image:demo | head -c %d > out.jpg" %
                (PORT, image_size))
        rc, _ = run("redis-cli -p %d --raw GET image:demo | head -c %d > out.jpg" %
                    (PORT, image_size), cwd=tmpdir)
        if rc != 0:
            raise SystemExit("binary GET failed")

        command("cmp -s demo.jpg out.jpg && echo identical")
        output(run("cmp -s demo.jpg out.jpg && echo identical", cwd=tmpdir)[1], "ok")

        command("shasum -a 256 demo.jpg out.jpg")
        output(run("shasum -a 256 demo.jpg out.jpg", cwd=tmpdir)[1])

        command("redis-cli -p %d SWITCH_POLICY sieve" % PORT)
        output(run(["redis-cli", "-p", str(PORT), "SWITCH_POLICY", "sieve"])[1], "ok")

        command(
            "curl -s http://127.0.0.1:%d/metrics | python3 -c \"import json,sys; "
            "m=json.load(sys.stdin); print(json.dumps({k: m[k] for k in "
            "('total_keys','used_memory','hit_rate','evictions')}, indent=2))\"" % ADMIN)
        out = run(
            "curl -s http://127.0.0.1:%d/metrics | python3 -c \"import json,sys; "
            "m=json.load(sys.stdin); print(json.dumps({k: m[k] for k in "
            "('total_keys','used_memory','hit_rate','evictions')}, indent=2))\"" % ADMIN)
        output(out[1], "blue")

        lines = []
        for kind, text in events:
            color = {"cmd": TEXT, "ok": GREEN, "dim": DIM,
                     "blue": BLUE, "out": DIM, "err": RED}[kind]
            wrapped = textwrap.wrap(text, width=WRAP, replace_whitespace=False,
                                    drop_whitespace=False)
            if not wrapped:
                wrapped = [""]
            for index, part in enumerate(wrapped):
                prefix = "" if index == 0 or kind != "cmd" else "  "
                lines.append((prefix + part, color, kind))

        frames = []
        durations = []
        max_lines = (HEIGHT - TOP - 18) // LINE_HEIGHT

        def render(visible, cursor=False):
            image = Image.new("RGB", (WIDTH, HEIGHT), BG)
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle([0, 0, WIDTH - 1, HEIGHT - 1], radius=12,
                                   fill=BG, outline=BORDER)
            draw.rectangle([0, 0, WIDTH, 46], fill=BAR)
            for index, color in enumerate(((255, 95, 86), (255, 189, 46),
                                           (39, 201, 63))):
                draw.ellipse([20 + index * 22, 18, 32 + index * 22, 30],
                             fill=color)
            draw.text((WIDTH // 2, 23), "polycache  |  binary blob cache",
                      font=TITLE_FONT, fill=DIM, anchor="mm")

            y = TOP
            window = visible[-max_lines:] if len(visible) > max_lines else visible
            for index, (text, color, kind) in enumerate(window):
                x = 24
                if kind == "cmd" and text.startswith("$ "):
                    draw.text((x, y), "$ ", font=MONO, fill=GREEN)
                    x += draw.textlength("$ ", font=MONO)
                    text = text[2:]
                draw.text((x, y), text, font=MONO, fill=color)
                if cursor and index == len(window) - 1:
                    end = x + draw.textlength(text, font=MONO)
                    draw.rectangle([end + 2, y + 2, end + 10, y + 18],
                                   fill=GREEN)
                y += LINE_HEIGHT

            draw.text((24, HEIGHT - 20),
                      "RESP2  |  redis-cli compatible  |  values up to 512MB",
                      font=TITLE_FONT, fill=(90, 100, 110))
            return image.quantize(colors=48, method=Image.MEDIANCUT)

        for index, line in enumerate(lines):
            if line[2] == "cmd" and len(line[0]) > 8:
                partial = (line[0][:max(8, len(line[0]) * 2 // 3)], line[1], "cmd")
                frames.append(render(lines[:index] + [partial], cursor=True))
                durations.append(140)
            frames.append(render(lines[:index + 1], cursor=True))
            durations.append(140)

        frames.append(render(lines, cursor=False))
        durations.append(2400)

        gif_path = os.path.join(ROOT, "demo.gif")
        frames[0].save(gif_path, save_all=True, append_images=frames[1:],
                       duration=durations, loop=0, optimize=True, disposal=2)
        print("wrote %s (%d frames, %.1f KB)" %
              (gif_path, len(frames), os.path.getsize(gif_path) / 1024.0))
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
        for path in (image_path, out_path):
            try:
                os.remove(path)
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
