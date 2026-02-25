"""pytest 루트 conftest — 프로젝트 루트를 sys.path에 추가한다."""
import sys
from pathlib import Path

# CI/로컬 모두 'modules', 'server', 'scripts' 패키지를 임포트할 수 있도록
# 프로젝트 루트를 sys.path 맨 앞에 삽입한다.
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
