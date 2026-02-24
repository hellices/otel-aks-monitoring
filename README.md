# AG-UI on AKS — Azure AI Foundry 폐쇄망 환경

AG-UI(Agent-User Interaction Protocol) 기반 AI 에이전트를 AKS 폐쇄망 환경에서 Azure AI Foundry와 Private Endpoint로 연결하여 운영하는 프로젝트.

## 핵심 구성

| 영역 | 기술 |
|---|---|
| **에이전트** | Microsoft Agent Framework + FastAPI, AG-UI SSE 스트리밍 |
| **LLM** | Azure AI Foundry `gpt-5.2-chat`, Private Endpoint 전용 |
| **인증** | AKS Service Connector + Workload Identity (자격 증명 하드코딩 없음) |
| **모니터링** | OTel Operator 자동 계측 → OTel Collector → Azure Managed Prometheus → Grafana |
| **네트워크** | 모든 PaaS Private Endpoint, Azure Firewall DNS Proxy, AMPLS |

---

## 문서 읽는 순서

| # | 문서 | 설명 |
|---|---|---|
| 1 | **[아키텍처 개요](docs/architecture.md)** | 전체 시스템 구성, 네트워크, Service Connector, 앱 구조, 보안 |
| 2 | **[모니터링 구축 가이드](docs/monitoring.md)** | OTel 스택 설치, AMPLS/Private Link 구성, 앱 자동 계측, 트러블슈팅 |

---

## 파일 구조

```
workspace/
├── README.md                              ← 지금 보고 있는 파일
│
├── docs/
│   ├── architecture.md                    # 아키텍처 개요 (플랫폼, 네트워크, 보안)
│   └── monitoring.md                      # 모니터링 구축 가이드 (OTel, AMPLS, 디버깅)
│
├── aks/
│   └── monitor/
│       └── pod-monitor.yaml               # 앱 네임스페이스용 PodMonitor (azmonitoring.coreos.com)
│
├── infra/
│   ├── opentelemetry-operator.yaml        # OTel Operator 전체 매니페스트 (CRD, RBAC, Webhook)
│   └── otel-collector.yaml                # OTel Collector CR + PodMonitor
│
└── myapp/
    ├── server.py                          # AG-UI FastAPI 서버 (OpenAI instrumentor + 커스텀 메트릭)
    ├── requirements.txt                   # Python 의존성
    ├── Dockerfile                         # 컨테이너 이미지 빌드 (python:3.12-slim)
    ├── k8s-deploy.yaml                    # Deployment + Service
    ├── otel-instrumentation.yaml          # OTel Python 자동 계측 Instrumentation CR
    └── static/
        └── index.html                     # 브라우저 채팅 UI
```

---

## Quick Reference

| 항목 | 값 |
|---|---|
| AKS 클러스터 | `aks-contoso-koreacentral-01` (Private Cluster) |
| AI Foundry | `aif-contoso-krc-01` (gpt-5.2-chat) |
| ACR | `acrcontosokrc01` (Premium, Private Endpoint) |
| UAMI | `uami-aif-contoso-krc-01` (clientId: `0429ea37-...`) |
| App Namespace | `myapp` |
| OTel Namespace | `opentelemetry-operator-system` |
| Subscription | `f752aff6-b20c-4973-b32b-0a60ba2c6764` |

---

## 배포 명령

```bash
# 인프라 (최초 1회)
kubectl apply -f infra/opentelemetry-operator.yaml
kubectl apply -f infra/otel-collector.yaml

# 앱 네임스페이스 PodMonitor (Azure RBAC 권한 필요 — docs/monitoring.md 6.5 참고)
kubectl apply -f aks/monitor/pod-monitor.yaml

# 앱
kubectl apply -f myapp/otel-instrumentation.yaml
kubectl apply -f myapp/k8s-deploy.yaml

# 이미지 빌드 & 푸시
cd myapp
podman build -t acrcontosokrc01.azurecr.io/myapp/agui-server:latest .
podman push acrcontosokrc01.azurecr.io/myapp/agui-server:latest
kubectl rollout restart deploy/agui-server -n myapp
```

---

## 수집 중인 메트릭

| 메트릭 | 타입 | 설명 | scope |
|---|---|---|---|
| `gen_ai_client_operation_duration_seconds` | histogram | Gen AI (OpenAI) 호출 latency | openai_v2 instrumentor |
| `agui_agent_request_count_total` | counter | AG-UI 에이전트 요청 수 | 커스텀 (server.py) |
| `http_server_duration_milliseconds` | histogram | HTTP 요청 처리 시간 | FastAPI auto-instrumentation |
| `http_server_active_requests` | gauge | 현재 활성 HTTP 요청 수 | FastAPI auto-instrumentation |
| `http_server_response_size_bytes` | histogram | HTTP 응답 크기 | FastAPI auto-instrumentation |
