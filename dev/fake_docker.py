"""Заглушка Docker для локальной проверки NOEMA Cleaner без Home Assistant."""

import re

MB = 1024 * 1024


class _Result:
    def __init__(self, code, out):
        self.exit_code = code
        self.output = out.encode()


class FakeContainer:
    def __init__(self, name, image, image_id, sizes=None, rw=0):
        self.name = name
        self.id = "c-" + name
        self.attrs = {"Image": image_id, "Config": {"Image": image}}
        self.sizes = dict(sizes or {})  # путь -> байты
        self.rw = rw

    def _under(self, path):
        return [p for p in self.sizes if p == path or p.startswith(path + "/")]

    def exec_run(self, argv):
        cmd = argv[-1]
        if m := re.search(r"du -sxk '([^']+)'", cmd):
            size = sum(self.sizes[p] for p in self._under(m.group(1)))
            return _Result(0, f"{size // 1024}\t{m.group(1)}\n" if size else "")
        if m := re.search(r"ls -1 '([^']+)'", cmd):
            prefix = m.group(1) + "/"
            names = {p[len(prefix):].split("/")[0] for p in self.sizes if p.startswith(prefix)}
            return _Result(0, "".join(n + "\n" for n in sorted(names)))
        if m := re.search(r"find '([^']+)' -mindepth 1", cmd):
            for p in [p for p in self.sizes if p.startswith(m.group(1) + "/")]:
                del self.sizes[p]
            return _Result(0, "")
        if m := re.search(r"rm -rf '([^']+)'", cmd):
            for p in self._under(m.group(1)):
                del self.sizes[p]
            return _Result(0, "")
        return _Result(1, "unsupported: " + cmd)


class FakeImage:
    def __init__(self, image_id, tags, size):
        self.id = image_id
        self.tags = tags
        self.attrs = {"Size": size}


class FakeDocker:
    def __init__(self):
        self.buildcache = 800 * MB
        self.image_store = [
            FakeImage("img-esphome", ["ghcr.io/esphome/esphome-hassio:2024.9"], 900 * MB),
            FakeImage("img-tessera", ["local/esp_screen_manager:1.0"], 1200 * MB),
            FakeImage("img-dist", ["local/esphome-dist-server:1.0"], 600 * MB),
            FakeImage("img-self", ["local/noema_cleaner:0.4.0"], 150 * MB),
            FakeImage("img-dangling", [], 300 * MB),
            FakeImage("img-old", ["local/old_app:0.1"], 400 * MB),
            FakeImage("img-base", ["ghcr.io/home-assistant/amd64-base:3.20"], 20 * MB),
        ]
        self.container_store = [
            FakeContainer("addon_5c53de3b_esphome", "ghcr.io/esphome/esphome-hassio:2024.9", "img-esphome", {
                "/data/build/dev1/obj": 700 * MB,
                "/data/packages/x": 40 * MB,
                "/data/cache/idf/ccache/a": 300 * MB,
                "/data/cache/platformio/cache/z": 90 * MB,
                "/data/cache/platformio/packages/toolchain": 2000 * MB,
            }, rw=120 * MB),
            FakeContainer("addon_local_esp_screen_manager", "local/esp_screen_manager:1.0", "img-tessera", {
                "/data/build/s1": 500 * MB,
                "/data/esphome/packages/y": 20 * MB,
            }, rw=10 * MB),
            FakeContainer("addon_local_esphome_dist_server", "local/esphome-dist-server:1.0", "img-dist", {
                f"/data/esphome-versions/{v}/bin": 200 * MB for v in ("2024.6.0", "2024.7.0", "2024.9.0")
            }),
            FakeContainer("addon_core_mariadb", "homeassistant/amd64-addon-mariadb:2.7", "img-old", {
                "/data/databases/ibdata": 3000 * MB,
            }, rw=5 * MB),
            FakeContainer("addon_local_noema_cleaner", "local/noema_cleaner:0.4.0", "img-self", {}),
            FakeContainer("hassio_supervisor", "ghcr.io/home-assistant/amd64-hassio-supervisor", "img-self", {}),
        ]
        self.images = _Images(self)
        self.containers = _Containers(self)
        self.api = _Api(self)

    def ping(self):
        return True

    def df(self):
        return {
            "LayersSize": sum(i.attrs["Size"] for i in self.image_store),
            "BuildCache": [{"Size": self.buildcache, "InUse": False}] if self.buildcache else [],
            "Containers": [{"SizeRw": c.rw} for c in self.container_store],
        }


class _Images:
    def __init__(self, d):
        self.d = d

    def list(self, filters=None):
        if filters and filters.get("dangling"):
            return [i for i in self.d.image_store if not i.tags]
        return list(self.d.image_store)

    def prune(self, filters=None):
        self.d.image_store = [i for i in self.d.image_store if i.tags]

    def remove(self, image_id):
        self.d.image_store = [i for i in self.d.image_store if i.id != image_id]


class _Containers:
    def __init__(self, d):
        self.d = d

    def list(self, all=False):
        return list(self.d.container_store)

    def run(self, image, cmd, **kwargs):
        # du -sxk выводит КБ и путь через табуляцию, как настоящий HOST_DU
        return (b"512000\t/d/supervisor/backup\n20480\t/d/logs\n10240\t/d/supervisor/share\n")


class _Api:
    def __init__(self, d):
        self.d = d

    def containers(self, size=False):
        return [{"Id": c.id, "SizeRw": c.rw} for c in self.d.container_store]

    def _url(self, path):
        return path

    def _post(self, url, params=None):
        self.d.buildcache = 0

    def _raise_for_status(self, r):
        pass
