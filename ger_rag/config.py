import json
import logging
import math
import os
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path

logger = logging.getLogger(__name__)

# Config file locations (checked in order):
#   1. GER_RAG_CONFIG env var
#   2. ~/.config/ger-rag/config.json  (Linux/macOS)
#      %APPDATA%/ger-rag/config.json  (Windows)
_CONFIG_FILE_PATHS = []

_env_config = os.environ.get("GER_RAG_CONFIG")
if _env_config:
    _CONFIG_FILE_PATHS.append(Path(_env_config))

if sys.platform == "win32":
    _appdata = os.environ.get("APPDATA", "")
    if _appdata:
        _CONFIG_FILE_PATHS.append(Path(_appdata) / "ger-rag" / "config.json")
else:
    _xdg = os.environ.get("XDG_CONFIG_HOME", "")
    _config_base = Path(_xdg) if _xdg else Path.home() / ".config"
    _CONFIG_FILE_PATHS.append(_config_base / "ger-rag" / "config.json")


def _load_config_file() -> dict:
    """Load config overrides from JSON config file."""
    for path in _CONFIG_FILE_PATHS:
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                logger.info("Loaded config from %s", path)
                return data
            except (json.JSONDecodeError, OSError):
                pass
    return {}


def _default_data_dir() -> str:
    """Resolve data directory.

    Priority:
      1. GER_RAG_DATA_DIR env var
      2. "data_dir" in config file
      3. Platform default:
         - Linux/macOS: ~/.local/share/ger-rag/
         - Windows:     %LOCALAPPDATA%/ger-rag/
    """
    env = os.environ.get("GER_RAG_DATA_DIR")
    if env:
        p = Path(env)
    else:
        file_conf = _load_config_file()
        if "data_dir" in file_conf:
            p = Path(file_conf["data_dir"])
        elif sys.platform == "win32":
            local = os.environ.get("LOCALAPPDATA", "")
            p = Path(local) / "ger-rag" if local else Path.home() / "ger-rag"
        else:
            xdg = os.environ.get("XDG_DATA_HOME", "")
            base = Path(xdg) if xdg else Path.home() / ".local" / "share"
            p = base / "ger-rag"
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


@dataclass
class GERConfig:
    # Embedding
    model_name: str = "cl-nagoya/ruri-v3-310m"
    embedding_dim: int = 768
    batch_size: int = 32

    # Retrieval
    top_k: int = 10

    # Scoring
    alpha: float = 0.05       # Mass boost scaling
    delta: float = 0.01       # Temporal decay rate
    gamma: float = 0.5        # Temperature scaling

    # Mass update
    eta: float = 0.05         # Mass growth rate
    m_max: float = 50.0       # Mass saturation limit

    # Co-occurrence graph
    edge_threshold: int = 5   # Co-occurrence count to form edge
    edge_decay: float = 0.97  # Edge weight decay factor
    prune_threshold: float = 0.5  # Edge removal threshold
    max_degree: int = 20      # Per-node edge cap

    # Gravitational displacement
    gravity_G: float = 0.01            # Gravitational constant
    gravity_eta: float = 0.005         # Displacement learning rate
    gravity_epsilon: float = 1e-6      # Zero-division guard
    displacement_decay: float = 0.995  # Per-step displacement decay
    displacement_age_delta: float = 0.005  # Access-age based decay rate
    max_displacement_norm: float = 0.3 # Max L2 norm of displacement vector

    # Gravity wave propagation
    wave_initial_k: int = 3            # Initial FAISS top-k for seed nodes
    wave_max_depth: int = 2            # Maximum recursion depth
    wave_base_k: int = 2              # Minimum neighbors per node
    wave_mass_scale: float = 2.0       # Mass-to-top-k scaling factor
    wave_max_node_k: int = 10          # Maximum neighbors per node
    wave_attenuation: float = 0.3      # Force decay per depth level
    wave_mass_attenuation_factor: float = 0.5  # Mass-based attenuation reduction
    wave_boost_weight: float = 0.05    # Wave force weight in final score

    # Similarity history
    sim_buffer_size: int = 20  # Ring buffer size

    # Storage — resolved to fixed data directory
    data_dir: str = field(default_factory=_default_data_dir)
    db_path: str = ""
    faiss_index_path: str = ""

    # Write-behind
    flush_interval_seconds: float = 5.0
    flush_threshold: int = 100

    def __post_init__(self):
        if not self.db_path:
            self.db_path = os.path.join(self.data_dir, "ger_rag.db")
        if not self.faiss_index_path:
            self.faiss_index_path = os.path.join(self.data_dir, "ger_rag.faiss")

    @classmethod
    def from_config_file(cls) -> "GERConfig":
        """Create GERConfig with overrides from config.json."""
        file_conf = _load_config_file()
        valid_fields = {f.name for f in fields(cls)}
        overrides = {k: v for k, v in file_conf.items() if k in valid_fields}
        return cls(**overrides)

    def compute_node_top_k(self, mass: float) -> int:
        """Compute per-node top-k based on mass."""
        k = self.wave_base_k + int(self.wave_mass_scale * math.log(1.0 + mass))
        return min(k, self.wave_max_node_k)

    def compute_effective_attenuation(self, mass: float) -> float:
        """Compute effective attenuation (high mass = slower decay = farther reach)."""
        mass_factor = math.log(1.0 + mass) / math.log(1.0 + self.m_max)
        return self.wave_attenuation * (1.0 - self.wave_mass_attenuation_factor * mass_factor)
