# 구성과 원본

이 저장소는 `pickle-air-mcp-0.1.0` 배포본을 고친 버전입니다. 그 배포본은 `pickle-air-cli-0.1.0.zip`의 CLI를 포함합니다.

원본 CLI ZIP SHA-256: `8739555a191c303fd105349d82c59f60729fc8b12fbf690584e11c7f1501983d`

## 원본과 달라진 파일

- `pickle_cli/core.py`, `pickle_cli/__main__.py`, `pickle_cli/clipboard.swift` — 답변 복사, 스크롤 읽기, 사진 받기, 사용량 읽기, 한글 입력, 화면 크기 대응
- `pickle_mcp/server.py`, `pickle_mcp/__main__.py` — `pickle_download_image`, `pickle_usage`, `pickle_call` 추가와 `pickle_read` 인자 확장
- `tests/test_mcp.py`, 루트 문서

바뀐 내용은 [LOCAL-CHANGES.md](LOCAL-CHANGES.md)에 있습니다.

## 원본 그대로인 파일

- `pickle_cli/__init__.py`, `pickle_cli/adb.py`, `pickle_mcp/__init__.py`, `pickle_mcp/cli_bridge.py`
- 루트 `pickle-air`, `LICENSE`, `examples/test-data.txt`, `examples/test-image.png`

원본 CLI 문서는 `docs/README.md`, `docs/SECURITY.md`, `docs/TESTING.md`에 있고, 원본 단위 테스트는 `tests/test_original_cli.py`입니다. 이 문서들은 원본 시점의 설명이라 지금 코드와 다른 부분이 있습니다.

배포본에 들어 있던 `MANIFEST.sha256`은 수정 전 파일의 체크섬이라 저장소에 넣지 않았습니다.
