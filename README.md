# AKS Private Cluster — Azure AI Foundry 폐쇄망 환경

AKS 폐쇄망 환경에서 Azure AI Foundry와 Private Endpoint로 연결하여 AI 앱을 운영하는 프로젝트. 두 가지 모니터링 패턴을 병행 운영.

## 핵심 구성

| 영역 | 기술 |
|---|---|
| **앱 (myapp)** | Microsoft Agent Framework + FastAPI, AG-UI SSE 스트리밍 |
| **앱 (otel-app)** | question-app — LangGraph 기반 Teacher-Student 퀴즈 시스템 ([hellices/otel-langfuse](https://github.com/hellices/otel-langfuse)) |
| **LLM** | Azure AI Foundry `gpt-5.2-chat`, Private Endpoint 전용 |
| **인증** | AKS Service Connector + Workload Identity (자격 증명 하드코딩 없음) |
| **모니터링** | OTel Operator → OTel Collector (패턴 2가지: 아래 참고) |
| **네트워크** | 모든 PaaS Private Endpoint, Azure Firewall DNS Proxy, AMPLS |

### 모니터링 패턴 비교

| | myapp (Prometheus/Grafana) | otel-app (Application Insights) |
|---|---|---|
| **Collector 위치** | `opentelemetry-operator-system` | `otel-app` |
| **트레이스** | OTel Collector → prometheus exporter | OTel Collector → `azuremonitor` exporter → App Insights |
| **메트릭** | OTel Collector → prometheus exporter → ama-metrics → Managed Prometheus → Grafana | OTel Collector → `azuremonitor` exporter → App Insights + prometheus exporter → ama-metrics |
| **확인 도구** | Azure Managed Grafana | Application Insights Transaction Search / Application Map / Metrics Explorer + Grafana |
| **인증** | DCR/DCE 자동 (ama-metrics addon) | Application Insights Connection String (Secret) |
| **Private Link** | AMPLS → ama-metrics가 DCE 경유 | AMPLS → Collector가 Ingestion Endpoint 경유 |

---

## 문서 읽는 순서

| # | 문서 | 설명 |
|---|---|---|
| 1 | **[아키텍처 개요](docs/architecture.md)** | 전체 시스템 구성, 네트워크, Service Connector, 앱 구조, 보안 |
| 2 | **[모니터링 개요](docs/monitoring.md)** | 공통 인프라 (OTel Operator, AMPLS), 두 방식 비교 |
| 2-a | **[방식 A: Prometheus / Grafana](docs/monitoring-prometheus.md)** | myapp — auto-inject, Prometheus exporter, Grafana |
| 2-b | **[방식 B: Application Insights](docs/monitoring-appinsights.md)** | otel-app — azuremonitor exporter, App Insights + Grafana |
| 3 | **[서비스 가이드](docs/service.md)** | myapp API 명세, 인증 흐름, 배포 구성, 운영 가이드 |

---

## 파일 구조

```
workspace/
├── README.md                              ← 지금 보고 있는 파일
│
├── docs/
│   ├── architecture.md                    # 아키텍처 개요 (플랫폼, 네트워크, 보안)
│   ├── monitoring.md                      # 모니터링 개요 (공통 인프라, 방식 비교)
│   ├── monitoring-prometheus.md            # 방식 A: Prometheus / Grafana (myapp 상세)
│   ├── monitoring-appinsights.md           # 방식 B: Application Insights (otel-app 상세)
│   └── service.md                         # 서비스 가이드 (myapp API, 인증, 운영)
│
├── infra/
│   ├── opentelemetry-operator.yaml        # OTel Operator 전체 매니페스트 (CRD, RBAC, Webhook)
│   ├── otel-collector.yaml                # OTel Collector CR + PodMonitor (otel-app 네임스페이스)
│   └── azure-monitor-secret.yaml          # App Insights Connection String Secret (gitignore 대상)
│
├── myapp/                                 # 패턴 A: Prometheus/Grafana 모니터링
│   ├── server.py                          # AG-UI FastAPI 서버
│   ├── requirements.txt
│   ├── Dockerfile
│   ├── k8s-deploy.yaml                    # Deployment + Service
│   ├── otel-instrumentation.yaml          # OTel Python 자동 계측 Instrumentation CR
│   └── static/
│       └── index.html                     # 브라우저 채팅 UI
│
└── otel-app/                              # 패턴 B: Application Insights 모니터링
    └── deployment.yaml                    # question-app Deployment + Service
```

---

## Quick Reference

| 항목 | 값 |
|---|---|
| AKS 클러스터 | `aks-contoso-koreacentral-01` (Private Cluster) |
| AI Foundry | `aif-rubicon-krc-01` (gpt-5.2-chat) |
| ACR | `acrcontosokrc01` (Premium, Private Endpoint) |
| UAMI | `uami-aif-contoso-krc-01` (clientId: `0429ea37-...`) |
| myapp Namespace | `myapp` — Prometheus/Grafana 패턴 |
| otel-app Namespace | `otel-app` — Application Insights 패턴 |
| OTel Operator Namespace | `opentelemetry-operator-system` |
| Subscription | `f752aff6-b20c-4973-b32b-0a60ba2c6764` |

---

## 배포 명령

### 인프라 (최초 1회)
```bash
kubectl apply -f infra/opentelemetry-operator.yaml
kubectl apply -f infra/azure-monitor-secret.yaml   # gitignore 대상, App Insights 연결 문자열
kubectl apply -f infra/otel-collector.yaml          # otel-app 네임스페이스에 Collector + PodMonitor
```

### myapp (Prometheus/Grafana 패턴)
```bash
kubectl apply -f myapp/otel-instrumentation.yaml
kubectl apply -f myapp/k8s-deploy.yaml

# 이미지 빌드 & 푸시
cd myapp
podman build -t acrcontosokrc01.azurecr.io/myapp/agui-server:latest .
podman push acrcontosokrc01.azurecr.io/myapp/agui-server:latest
kubectl rollout restart deploy/agui-server -n myapp
```

### otel-app (Application Insights 패턴)

> **이미지 소스**: [`hellices/otel-langfuse`](https://github.com/hellices/otel-langfuse) — LangGraph 기반 Teacher-Student 퀴즈 시스템.

```bash
kubectl apply -f otel-app/deployment.yaml
```

---

## 수집 중인 메트릭

### myapp → Managed Prometheus / Grafana

| 메트릭 | 타입 | 설명 | scope |
|---|---|---|---|
| `gen_ai_client_operation_duration_seconds` | histogram | Gen AI (OpenAI) 호출 latency | openai_v2 instrumentor |
| `agui_agent_request_count_total` | counter | AG-UI 에이전트 요청 수 | 커스텀 (server.py) |
| `http_server_duration_milliseconds` | histogram | HTTP 요청 처리 시간 | FastAPI auto-instrumentation |
| `http_server_active_requests` | gauge | 현재 활성 HTTP 요청 수 | FastAPI auto-instrumentation |
| `http_server_response_size_bytes` | histogram | HTTP 응답 크기 | FastAPI auto-instrumentation |

### otel-app → Application Insights

question-app([hellices/otel-langfuse](https://github.com/hellices/otel-langfuse))이 앱 내장 OTel SDK로 traces/metrics를 Collector에 전송, `azuremonitor` exporter를 통해 Application Insights로 전달. Application Insights에서 확인:
- **Transaction Search**: 개별 트레이스/요청 상세
- **Application Map**: 서비스 간 의존성 토폴로지
- **Metrics Explorer**: 커스텀 메트릭 시각화
- **Managed Grafana**: PodMonitor 경유 Prometheus 메트릭도 병행 수집 ([Grafana 대시보드 JSON](https://github.com/hellices/otel-langfuse/tree/main/k8s) 제공)
