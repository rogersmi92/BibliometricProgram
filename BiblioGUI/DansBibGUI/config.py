from pathlib import Path


APP_TITLE = "DansBib GUI"
DEFAULT_SOURCES = {
    "pubmed": True,
    "openalex": True,
    "wos": True,
    "scopus": True,
    "covidence": False,
}
SOURCE_LABELS = {
    "pubmed": "PubMed",
    "openalex": "OpenAlex",
    "wos": "Web of Science",
    "scopus": "Scopus",
    "covidence": "Covidence RIS",
}
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DANSBIB_ROOT = PROJECT_ROOT / "DansBib"
DANSBIB_DATA_DIR = DANSBIB_ROOT / "data"
DANSBIB_OUTPUTS_DIR = DANSBIB_DATA_DIR / "outputs"
DANSBIB_VISUALS_DIR = DANSBIB_DATA_DIR / "visuals"
DANSBIB_VOS_DIR = DANSBIB_DATA_DIR / "VOS"
LOG_POLL_INTERVAL_MS = 100
