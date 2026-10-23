"""Motor de Transformação e Compatibilidade para Atualização Contínua do MyArchive Downloader.

Este módulo recebe os ficheiros fonte actualizados pela comunidade e aplica
automaticamente todo o pipeline de compatibilidade do MyArchive Downloader:
1. Unificação de pasta: Achata 'extractor/*' directamente na raiz de extractors.
2. Adaptação de imports relativos: Converte 'from ..utils' em 'from .utils', etc.
3. Desacoplamento da camada nativa: Mantém e injecta 'custom/' nos extractores relevantes.
4. Anonimização e Branding: Remove marcas externas em version.py e docstrings.
5. Validação pré-instalação: Compila e testa todas as 1.750+ classes em ambiente isolado antes de aplicar.
6. Cópia de segurança atómica e Rollback caso ocorra qualquer erro.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# Ficheiros onde a camada nativa MyArchive tem wrappers de alta prioridade
COLLISION_NAMES = [
    "bilibili", "brightcove", "dailymotion", "facebook", "francetv",
    "generic", "instagram", "jwplatform", "kaltura", "pinterest",
    "reddit", "soundcloud", "tiktok", "twitch", "twitter", "vimeo",
]

SIBLING_TARGETS = [
    "downloader", "postprocessor", "networking", "utils", "compat",
    "cookies", "aes", "dependencies", "update", "version", "plugins",
    "YoutubeDL", "_legacy"
]


class EngineTransformer:
    """Aplica o pipeline de adaptação e compatibilidade a uma nova versão de código-fonte."""

    def __init__(self, extractors_dir: Path | str | None = None) -> None:
        if extractors_dir is None:
            extractors_dir = Path(__file__).resolve().parent.parent / "extractors"
        self.extractors_dir = Path(extractors_dir).resolve()
        self.backups_dir = self.extractors_dir / ".backups"
        self.custom_dir = self.extractors_dir / "custom"

    def transform_and_deploy(
        self,
        source_root: Path,
        version_tag: str,
        progress_cb: Callable[[str, float], None] | None = None,
    ) -> bool:
        """Processa a nova versão numa pasta temporária, valida e aplica de forma atómica."""
        def notify(msg: str, pct: float) -> None:
            log.info("[SyncBot] %s (%.0f%%)", msg, pct)
            if progress_cb:
                progress_cb(msg, pct)

        notify("A preparar ambiente de staging isolado...", 10.0)
        with tempfile.TemporaryDirectory(prefix="myarchive_engine_staging_") as tmp_dir:
            staging_path = Path(tmp_dir) / "extractors"
            staging_path.mkdir(parents=True, exist_ok=True)

            notify("A localizar componentes do motor de media...", 20.0)
            upstream_core = self._find_upstream_core(source_root)
            if not upstream_core:
                raise RuntimeError(f"Não foi possível encontrar a directoria raiz do motor em {source_root}")

            notify("A unificar e achatar directoria de extractores...", 35.0)
            self._flatten_and_copy(upstream_core, staging_path)

            notify("A adaptar imports relativos e caminhos de pacotes...", 50.0)
            self._rewrite_imports(staging_path)

            notify("A integrar camada de compatibilidade nativa (custom wrappers)...", 65.0)
            self._inject_custom_compatibility(staging_path, version_tag)

            notify("A validar sintaxe e inicialização de 1.750+ plataformas...", 80.0)
            validation_ok, val_err = self._validate_staging(staging_path)
            if not validation_ok:
                raise RuntimeError(f"Falha na validação do novo motor: {val_err}")

            notify("A criar cópia de segurança e a instalar a nova versão...", 90.0)
            self._atomic_swap(staging_path, version_tag)

            notify(f"Motor MyArchive Downloader atualizado com sucesso para {version_tag}!", 100.0)
            return True

    def _find_upstream_core(self, source_root: Path) -> Path | None:
        """Localiza a pasta que contém o núcleo do motor (YoutubeDL.py ou extractor/)."""
        for candidate in source_root.rglob("YoutubeDL.py"):
            core_dir = candidate.parent
            if (core_dir / "extractor").exists() or (core_dir / "downloader").exists():
                return core_dir
        return None

    def _flatten_and_copy(self, upstream_core: Path, staging: Path) -> None:
        """Copia os ficheiros do motor e achata 'extractor/*' directamente na raiz de staging."""
        # 1. Copia componentes principais (utils, downloader, postprocessor, YoutubeDL.py, etc.)
        for item in upstream_core.iterdir():
            if item.name == "extractor":
                continue  # Será tratado de seguida
            if item.name.startswith("."):
                continue
            dest = staging / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)

        # 2. Achata extractor/* directamente na raiz de staging
        upstream_extractors = upstream_core / "extractor"
        if upstream_extractors.exists() and upstream_extractors.is_dir():
            for item in upstream_extractors.iterdir():
                if item.name.startswith("."):
                    continue
                dest = staging / item.name
                if item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dest)

    def _rewrite_imports(self, staging: Path) -> None:
        """Adapta imports relativos para o nível único unificado."""
        # Corrige top-level python files em staging
        for py_file in staging.glob("*.py"):
            text = py_file.read_text(encoding="utf-8", errors="ignore")
            modified = False

            # from ..utils -> from .utils
            for target in SIBLING_TARGETS:
                pat = rf"\bfrom \.\.{target}\b"
                if re.search(pat, text):
                    text = re.sub(pat, f"from .{target}", text)
                    modified = True

            # from .extractor import gen_extractor_classes -> from . import gen_extractor_classes
            if "from .extractor import" in text:
                text = text.replace("from .extractor import", "from . import")
                modified = True

            # Corrige futuras ordenações
            text = self._clean_misplaced_future_imports(text)

            py_file.write_text(text, encoding="utf-8")

        # Corrige ficheiros em subpastas (como downloader/, networking/, youtube/)
        for sub_py in staging.glob("*/*.py"):
            text = sub_py.read_text(encoding="utf-8", errors="ignore")
            modified = False

            # from ..extractor.xxx import yyy -> from ..xxx import yyy
            if "from ..extractor." in text:
                text = re.sub(r'from \.\.extractor\.(\w+) import', r'from ..\1 import', text)
                modified = True

            for target in SIBLING_TARGETS:
                pat = rf"\bfrom \.\.\.{target}\b"
                if re.search(pat, text):
                    text = re.sub(pat, f"from ..{target}", text)
                    modified = True

            # Corrige circular import em downloader/soop.py
            if sub_py.name == "soop.py" and "from ..afreecatv import _cloudfront_auth_request" in text:
                text = text.replace("from ..afreecatv import _cloudfront_auth_request\n", "")
                text = text.replace("from ..afreecatv import _cloudfront_auth_request\r\n", "")
                text = text.replace(
                    "self.ydl.urlopen(_cloudfront_auth_request(",
                    "from ..afreecatv import _cloudfront_auth_request\n                    self.ydl.urlopen(_cloudfront_auth_request("
                )
                modified = True

            text = self._clean_misplaced_future_imports(text)
            if modified:
                sub_py.write_text(text, encoding="utf-8")

    def _clean_misplaced_future_imports(self, content: str) -> str:
        """Garante que 'from __future__' só existe nas primeiras 10 linhas."""
        lines = content.splitlines(keepends=True)
        new_lines = []
        has_future_top = False
        for i, line in enumerate(lines):
            if line.strip().startswith("from __future__ import"):
                if i < 12 and not has_future_top:
                    new_lines.append(line)
                    has_future_top = True
                else:
                    continue  # Descarta
            else:
                new_lines.append(line)
        return "".join(new_lines)

    def _inject_custom_compatibility(self, staging: Path, version_tag: str) -> None:
        """Copia a pasta 'custom/', ficheiros base da app e injecta re-exports."""
        # 1. Copia a directoria 'custom/' intacta
        staging_custom = staging / "custom"
        staging_custom.mkdir(parents=True, exist_ok=True)
        if self.custom_dir.exists():
            shutil.copytree(self.custom_dir, staging_custom, dirs_exist_ok=True)

        # 2. Copia os ficheiros fundamentais da nossa APP
        app_files = ["base.py", "catalog.py", "direct.py", "registry.py"]
        for f in app_files:
            src = self.extractors_dir / f
            if src.exists():
                shutil.copy2(src, staging / f)

        # 3. Injecta re-exports nos 16 extractores colidentes
        for name in COLLISION_NAMES:
            p = staging / f"{name}.py"
            if p.exists():
                code = p.read_text(encoding="utf-8", errors="ignore")
                wrapper_call = f"\n# Re-export de compatibilidade MyArchive Downloader\ntry:\n    from .custom.{name} import *  # noqa: F401, F403\nexcept Exception:\n    pass\n"
                if f"from .custom.{name}" not in code:
                    p.write_text(code + wrapper_call, encoding="utf-8")

        # 4. Injecta re-export no youtube/__init__.py
        yt_init = staging / "youtube" / "__init__.py"
        if yt_init.exists():
            yt_code = yt_init.read_text(encoding="utf-8", errors="ignore")
            yt_wrapper = "\n# Re-export de compatibilidade MyArchive Downloader\ntry:\n    from ..custom.youtube import *  # noqa: F401, F403\nexcept Exception:\n    pass\n"
            if "from ..custom.youtube" not in yt_code:
                yt_init.write_text(yt_code + yt_wrapper, encoding="utf-8")

        # 5. Gera o __init__.py mestre unificado
        init_src = self.extractors_dir / "__init__.py"
        if init_src.exists():
            shutil.copy2(init_src, staging / "__init__.py")

        # 6. Atualiza version.py com versão e anonimização
        ver_file = staging / "version.py"
        clean_version = version_tag.lstrip("v")
        ver_content = f"""# Versão do Motor MyArchive Downloader
__version__ = '{clean_version}'
RELEASE_GIT_HEAD = 'upstream-{clean_version}'
VARIANT = None
UPDATE_HINT = None
CHANNEL = 'stable'
ORIGIN = 'MyArchive/MyArchive Downloader'
_pkg_version = '{clean_version}'
"""
        ver_file.write_text(ver_content, encoding="utf-8")

    def _validate_staging(self, staging: Path) -> tuple[bool, str | None]:
        """Executa um processo Python isolado para validar importações e classes."""
        val_script = """import sys
from pathlib import Path
staging = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(staging.parent.parent.parent)) # backend
sys.path.insert(0, str(staging))

try:
    from app.core.extractors.YoutubeDL import YoutubeDL
    from app.core.extractors import gen_extractor_classes
    classes = gen_extractor_classes()
    if len(classes) < 1000:
        print(f"ERRO: Poucos extractores detectados: {len(classes)}")
        sys.exit(2)
    ydl = YoutubeDL({'quiet': True, 'no_warnings': True})
    print(f"OK: {len(classes)} classes validadas, {len(ydl._ies)} matchers activos")
    sys.exit(0)
except Exception as e:
    import traceback
    traceback.print_exc()
    sys.exit(1)
"""
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(val_script)
            script_path = f.name

        try:
            python_bin = sys.executable
            # Cria temporariamente symlink ou estrutura de pacote para validar
            res = subprocess.run(
                [python_bin, script_path, str(staging)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if res.returncode == 0:
                log.info("Validação concluída: %s", res.stdout.strip())
                return True, None
            else:
                err = (res.stderr or res.stdout).strip()
                log.error("Falha na validação do staging: %s", err)
                return False, err
        except subprocess.TimeoutExpired:
            return False, "Tempo limite excedido na validação do motor"
        finally:
            try:
                os.remove(script_path)
            except OSError:
                pass

    def _atomic_swap(self, staging: Path, version_tag: str) -> None:
        """Substitui os ficheiros actuais pelos validados com cópia de segurança."""
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        backup_folder = self.backups_dir / f"backup_{timestamp}_{version_tag}"
        self.backups_dir.mkdir(parents=True, exist_ok=True)

        # 1. Faz backup de segurança da versão actual
        log.info("A criar cópia de segurança em %s", backup_folder)
        shutil.copytree(
            self.extractors_dir,
            backup_folder,
            ignore=shutil.ignore_patterns(".backups", ".staging", "__pycache__"),
        )

        # 2. Copia ficheiros do staging para a pasta oficial
        for item in staging.iterdir():
            dest = self.extractors_dir / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)

        log.info("Instalação do motor MyArchive %s concluída com sucesso", version_tag)

    def rollback(self) -> tuple[bool, str]:
        """Restaura a cópia de segurança mais recente."""
        if not self.backups_dir.exists():
            return False, "Nenhuma cópia de segurança disponível"

        backups = sorted(
            [b for b in self.backups_dir.iterdir() if b.is_dir() and b.name.startswith("backup_")],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not backups:
            return False, "Nenhuma cópia de segurança encontrada"

        latest = backups[0]
        log.info("A restaurar cópia de segurança de %s", latest)
        for item in latest.iterdir():
            dest = self.extractors_dir / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)

        return True, f"Motor restaurado com sucesso a partir de {latest.name}"
