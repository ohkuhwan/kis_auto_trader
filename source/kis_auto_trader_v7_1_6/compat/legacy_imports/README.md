# Legacy import shims

이 디렉터리의 파일은 이전 버전에서 사용하던 최상위 모듈 이름을 위한 호환 어댑터입니다.
V5.1.5 내부 코드는 `brokers/`, `trading/`, `storage/`, `strategies/`, `risk/`의 정식 모듈을 직접 사용합니다.
이전 외부 스크립트가 `import kis_api` 같은 최상위 import를 사용한다면 해당 shim을 프로젝트 루트에 복사하거나, 외부 스크립트의 import를 정식 패키지 경로로 변경하십시오.
