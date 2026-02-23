# 서비스 아키텍처

> AG-UI 채팅 서비스의 애플리케이션 구조, API 명세, 인증 흐름, 배포 구성, 운영 관점 가이드.
>
> 인프라(네트워크, Private Endpoint, Firewall 등)는 [architecture.md](architecture.md), 모니터링 파이프라인은 [monitoring.md](monitoring.md) 참조.

---

## 1. 서비스 토폴로지

```
┌─ 클라이언트 ─────────────────────────────────────────────────────────┐
│                                                                     │
│  브라우저  ──GET /chat/──▶  정적 HTML/JS (채팅 UI)                    │
│            ──POST /api/agent──▶  SSE 스트리밍 응답                    │
│                                                                     │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │  K8s Service        │
                    │  agui-server        │
                    │  ClusterIP :80      │
                    └──────────┬──────────┘
                               │ :8080
                    ┌──────────▼──────────┐
                    │  Deployment         │
                    │  agui-server        │
                    │  replicas: 1        │
                    │                     │
                    │  ┌───────────────┐  │
                    │  │ FastAPI       │  │
                    │  │ + Uvicorn     │  │
                    │  │ + OTel Auto   │  │
                    │  └───────┬───────┘  │
                    └──────────┼──────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                 ▼
   ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐
   │ AI Foundry   │  │ OTel         │  │ Azure AD         │
   │ OpenAI API   │  │ Collector    │  │ (Token 발급)      │
   │ PE:10.2.0.17 │  │ OTLP :4318   │  │ Workload Identity│
   │ gpt-5.2-chat │  │              │  │                  │
   └──────────────┘  └──────────────┘  └──────────────────┘
```

### 의존 서비스 요약

| 의존 서비스 | 프로토콜 | 용도 | 장애 시 영향 |
|---|---|---|---|
| AI Foundry (OpenAI) | HTTPS (PE) | LLM 추론 | 채팅 응답 불가 |
| Azure AD (OIDC) | HTTPS | Workload Identity 토큰 | 인증 실패, API 호출 불가 |
| OTel Collector | OTLP HTTP | 메트릭 전송 | 메트릭 유실 (앱 동작에는 무영향) |
| ACR | HTTPS (PE) | 이미지 Pull | 신규 배포/스케일링 불가 |

---

## 2. API 명세

### 2.1 엔드포인트

| Method | Path | Content-Type | 설명 |
|---|---|---|---|
| GET | `/` | — | → `/chat/` 리다이렉트 (302) |
| GET | `/chat/` | `text/html` | 채팅 UI (정적 HTML) |
| GET | `/chat/{file}` | — | 정적 파일 서빙 |
| POST | `/api/agent` | `application/json` → `text/event-stream` | AG-UI 에이전트 (SSE) |
| GET | `/docs` | `text/html` | Swagger UI (FastAPI 자동 생성) |
| GET | `/openapi.json` | `application/json` | OpenAPI 스펙 |

### 2.2 AG-UI 프로토콜 (`/api/agent`)

**Request:**
```json
{
  "messages": [
    { "role": "user", "content": "안녕하세요" }
  ]
}
```

**Response (SSE stream):**
```
data: {"type":"RUN_STARTED","threadId":"...","runId":"..."}
data: {"type":"TEXT_MESSAGE_START","messageId":"...","role":"assistant"}
data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"...","delta":"안녕"}
data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"...","delta":"하세요!"}
data: {"type":"TEXT_MESSAGE_END","messageId":"..."}
data: {"type":"RUN_FINISHED","threadId":"...","runId":"..."}
```

| 이벤트 타입 | 의미 |
|---|---|
| `RUN_STARTED` | 에이전트 실행 시작 |
| `TEXT_MESSAGE_START` | 어시스턴트 메시지 시작 |
| `TEXT_MESSAGE_CONTENT` | 텍스트 청크 (delta 스트리밍) |
| `TEXT_MESSAGE_END` | 메시지 완료 |
| `RUN_FINISHED` | 에이전트 실행 완료 |

### 2.3 에러 응답

| 상황 | HTTP Status | 원인 |
|---|---|---|
| 잘못된 요청 형식 | 422 | messages 누락 등 |
| AI Foundry 인증 실패 | 500 | Workload Identity 토큰 발급 실패 |
| AI Foundry 호출 실패 | 500 | 모델 배포 문제, 네트워크 단절 |
| 내부 서버 오류 | 500 | 앱 코드 예외 |

---

## 3. 인증 흐름 (앱 관점)

```
① Pod 시작 시
   Kubelet이 Projected ServiceAccount Token을 마운트
   (OIDC 발급자가 서명한 JWT)

② DefaultAzureCredential 초기화
   WorkloadIdentityCredential 자동 선택
   ↓ 환경변수 참조:
     AZURE_CLIENT_ID      ← SA 어노테이션에서 Webhook이 주입
     AZURE_TENANT_ID      ← Webhook이 주입
     AZURE_FEDERATED_TOKEN_FILE ← /var/run/secrets/.../token

③ AI Foundry API 호출 시
   azure-identity SDK가 Azure AD에 토큰 교환 요청:
     SA Token (JWT) → Azure AD → Access Token (Entra ID)

④ Access Token으로 AI Foundry 호출
   Authorization: Bearer <access_token>
   → AI Foundry가 RBAC(Cognitive Services OpenAI User) 확인
```

**앱 코드에서의 사용:**
```python
# 자격 증명 하드코딩 없음
credential = DefaultAzureCredential()  # ③ 자동 처리
chat_client = AzureOpenAIChatClient(
    credential=credential,
    endpoint=os.environ["AZURE_AISERVICES_OPENAI_BASE"],  # SC Secret에서 주입
    deployment_name="gpt-5.2-chat",
)
```

> Secret에는 endpoint URL만 저장됨 — API 키나 패스워드는 없음. 인증은 전적으로 Workload Identity 토큰 교환으로 처리.

---

## 4. 앱 구성 & 환경변수

### 4.1 Service Connector가 주입하는 환경변수

Secret `sc-myappaifconnection-secret`에서 `envFrom`으로 주입:

| 환경변수 | 값 (예시) | 용도 |
|---|---|---|
| `AZURE_AISERVICES_OPENAI_BASE` | `https://aif-contoso-krc-01.openai.azure.com/` | OpenAI API 엔드포인트 |
| `AZURE_AISERVICES_CLIENTID` | `0429ea37-bae6-...` | UAMI Client ID |
| `AZURE_AISERVICES_COGNITIVESERVICES_ENDPOINT` | `https://aif-contoso-krc-01.cognitiveservices.azure.com/` | Cognitive Services 엔드포인트 |
| `AZURE_AISERVICES_SPEECH_ENDPOINT` | `https://aif-contoso-krc-01.cognitiveservices.azure.com/` | Speech 엔드포인트 |

### 4.2 Deployment에서 직접 설정하는 환경변수

| 환경변수 | 값 | 용도 |
|---|---|---|
| `AZURE_OPENAI_DEPLOYMENT_NAME` | `gpt-5.2-chat` | 모델 배포 이름 |

### 4.3 OTel Operator가 주입하는 환경변수

Instrumentation CR + Webhook이 자동 주입:

| 환경변수 | 값 | 용도 |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-collector-collector.opentelemetry-operator-system.svc.cluster.local:4318` | OTLP 수집기 |
| `OTEL_METRICS_EXPORTER` | `otlp` | 메트릭 내보내기 방식 |
| `OTEL_TRACES_EXPORTER` | `none` | 트레이스 비활성화 |
| `OTEL_LOGS_EXPORTER` | `none` | 로그 비활성화 |
| `OTEL_SERVICE_NAME` | `agui-server` | 서비스 이름 |
| `OTEL_RESOURCE_ATTRIBUTES` | `service.namespace=myapp,...` | 리소스 속성 |

### 4.4 Workload Identity Webhook이 주입하는 환경변수

| 환경변수 | 용도 |
|---|---|
| `AZURE_CLIENT_ID` | Managed Identity Client ID |
| `AZURE_TENANT_ID` | Azure AD Tenant ID |
| `AZURE_FEDERATED_TOKEN_FILE` | SA 토큰 파일 경로 |
| `AZURE_AUTHORITY_HOST` | Azure AD 인증 URL |

---

## 5. 배포 구성

### 5.1 K8s 리소스 맵

```
Namespace: myapp
├── Deployment/agui-server          ← 직접 관리
│   └── Pod
│       ├── container: agui-server
│       └── initContainer: opentelemetry-auto-instrumentation-python  (OTel Operator 주입)
├── Service/agui-server (ClusterIP) ← 직접 관리
├── Instrumentation/python-instrumentation  ← 직접 관리
├── ServiceAccount/sc-account-0429ea37-...  ← Service Connector 자동 생성
├── Secret/sc-myappaifconnection-secret     ← Service Connector 자동 생성
└── FederatedIdentityCredential             ← Service Connector가 UAMI에 등록
```

### 5.2 리소스 제한

| 항목 | Request | Limit |
|---|---|---|
| CPU | 100m | 500m |
| Memory | 256Mi | 512Mi |

### 5.3 Health Checks

| 프로브 | Path | Port | 초기 대기 | 주기 |
|---|---|---|---|---|
| **Readiness** | `/chat/` | 8080 | 5s | 10s |
| **Liveness** | `/chat/` | 8080 | 10s | 30s |

> 두 프로브 모두 정적 파일 경로(`/chat/`)를 사용. FastAPI와 정적 파일 마운트가 정상 동작하면 200을 반환. AI Foundry 연결 상태는 체크하지 않음 (외부 의존성 장애 시 Pod 재시작 방지).

### 5.4 Node Scheduling

```yaml
tolerations:
  - key: "workload"
    operator: "Equal"
    value: "general"
    effect: "NoSchedule"
```

`workload=general` taint가 있는 노드에 스케줄링 (시스템 노드와 분리).

---

## 6. 빌드 & 배포

### 6.1 컨테이너 이미지

| 항목 | 값 |
|---|---|
| Base Image | `python:3.12-slim` |
| Registry | `acrcontosokrc01.azurecr.io` (Private Endpoint) |
| 이미지 경로 | `acrcontosokrc01.azurecr.io/myapp/agui-server:latest` |
| Pull 정책 | `Always` |

### 6.2 빌드 순서

```bash
# 1. 이미지 빌드 (Podman — Docker 호환)
podman build -t acrcontosokrc01.azurecr.io/myapp/agui-server:latest myapp/

# 2. ACR 로그인 & 푸시
az acr login -n acrcontosokrc01
podman push acrcontosokrc01.azurecr.io/myapp/agui-server:latest

# 3. K8s 배포
kubectl apply -f myapp/otel-instrumentation.yaml
kubectl apply -f myapp/k8s-deploy.yaml

# 4. 롤링 업데이트 (이미지 재빌드 후)
kubectl rollout restart deploy/agui-server -n myapp
```

### 6.3 의존성 (requirements.txt)

| 패키지 | 용도 | 비고 |
|---|---|---|
| `agent-framework-ag-ui` | MS Agent Framework + AG-UI 프로토콜 | `agent-framework` 포함 |
| `azure-identity` | `DefaultAzureCredential` (WI 인증) | |
| `fastapi` | 웹 프레임워크 | |
| `uvicorn[standard]` | ASGI 서버 | |
| `opentelemetry-api` | OTel 커스텀 메트릭 | |
| `opentelemetry-semantic-conventions-ai` | Gen AI 시맨틱 컨벤션 | **==0.4.13 고정 필수** |
| `opentelemetry-instrumentation-openai-v2` | OpenAI SDK 계측 | |

---

## 7. Observability (앱 관점)

### 7.1 수집 메트릭

| 메트릭 | 타입 | 소스 | 의미 |
|---|---|---|---|
| `gen_ai_client_operation_duration_seconds` | Histogram | openai-v2 instrumentor | LLM API 호출 지연 시간 |
| `agui_agent_request_count_total` | Counter | 커스텀 코드 | AG-UI 에이전트 요청 수 |
| `http_server_active_requests` | Gauge | FastAPI instrumentor | 현재 처리 중인 HTTP 요청 수 |
| `http_server_duration_milliseconds` | Histogram | FastAPI instrumentor | HTTP 요청 처리 시간 |

### 7.2 메트릭 경로

```
앱 코드 / OpenAI SDK / FastAPI
    │ OTel SDK (auto-instrumentation)
    ▼
OTLP HTTP → OTel Collector (:4318)
    │ Prometheus Exporter (:8889)
    ▼
ama-metrics (PodMonitor 스크래핑)
    │ AMPLS (Private Link)
    ▼
Azure Managed Prometheus → Grafana
```

### 7.3 주요 대시보드 쿼리

```promql
# LLM 평균 응답 시간 (초)
rate(gen_ai_client_operation_duration_seconds_sum{service_name="agui-server"}[5m])
  / rate(gen_ai_client_operation_duration_seconds_count{service_name="agui-server"}[5m])

# 에이전트 요청 RPS
rate(agui_agent_request_count_total{service_name="agui-server"}[5m])

# P99 HTTP 응답 시간
histogram_quantile(0.99, rate(http_server_duration_milliseconds_bucket{service_name="agui-server"}[5m]))

# 동시 접속 수
http_server_active_requests{service_name="agui-server"}
```

---

## 8. 운영 가이드

### 8.1 주요 확인 명령어

```bash
# Pod 상태
kubectl get po -n myapp -l app=agui-server

# 로그 확인
kubectl logs -n myapp -l app=agui-server -f

# 환경변수 전체 확인 (SC + OTel + WI 주입 포함)
kubectl exec -n myapp deploy/agui-server -- env | sort

# Service Connector Secret 내용 확인
kubectl get secret sc-myappaifconnection-secret -n myapp -o jsonpath='{.data}' \
  | python3 -c "import sys,json,base64; d=json.load(sys.stdin); [print(f'{k}={base64.b64decode(v).decode()}') for k,v in d.items()]"

# Readiness/Liveness 확인
kubectl exec -n myapp deploy/agui-server -- curl -s http://localhost:8080/chat/ | head -5

# API 동작 테스트
kubectl exec -n myapp deploy/agui-server -- \
  curl -s -N --max-time 15 http://localhost:8080/api/agent \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"messages":[{"role":"user","content":"hello"}]}' | head -10
```

### 8.2 장애 대응

| 증상 | 확인 사항 | 조치 |
|---|---|---|
| Pod `CrashLoopBackOff` | `kubectl logs` 확인 — `AZURE_AISERVICES_OPENAI_BASE` 미설정이면 SC Secret 확인 | SC Secret 존재 여부, `envFrom` 매핑 확인 |
| Pod Running이나 응답 없음 | Readiness probe 실패 여부 `kubectl describe pod` | 포트, 경로 확인 |
| SSE 응답 안 옴 | AI Foundry 연결 확인 — DNS 해석 → PE IP 반환 여부 | `nslookup aif-contoso-krc-01.openai.azure.com` |
| 인증 에러 (401/403) | Workload Identity 토큰 발급 확인 | SA 어노테이션, Federated Credential, RBAC 확인 |
| 메트릭 안 보임 | Collector 정상 여부, PodMonitor 설정 | [monitoring.md](monitoring.md) 참조 |

### 8.3 스케일링 고려사항

현재 `replicas: 1`로 단일 인스턴스. 스케일링 시 고려할 점:

- **Stateless**: 세션 상태 없음, 수평 확장 가능
- **SSE 연결**: 각 채팅은 하나의 long-lived HTTP 연결 — 로드밸런서가 연결 단위로 분배
- **AI Foundry 제한**: OpenAI API의 TPM/RPM 쿼터가 병목이 될 수 있음
- **OTel 수집기**: Collector가 단일 인스턴스이므로 트래픽 증가 시 Collector도 스케일링 필요

---

## 9. 파일 구조

```
myapp/
├── server.py               # FastAPI 앱 (AG-UI 엔드포인트, OTel 계측)
├── static/
│   └── index.html           # 채팅 UI (순수 HTML/JS, 빌드 도구 없음)
├── requirements.txt         # Python 의존성
├── Dockerfile               # 컨테이너 이미지 빌드
├── k8s-deploy.yaml          # Deployment + Service
└── otel-instrumentation.yaml  # OTel Instrumentation CR
```
