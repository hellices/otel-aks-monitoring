# 아키텍처 개요

> AKS Private Cluster에서 Azure AI Foundry를 Private Endpoint로 연결하고, Service Connector + Workload Identity로 자격 증명 없이 인증하는 폐쇄망 아키텍처.

---

## 1. 전체 구성도

```
┌─────────────────────────────────────────────────────────────────────┐
│  Azure Platform                                                     │
│                                                                     │
│  ┌──────────────────────┐    Private Endpoint     ┌───────────────┐ │
│  │ AI Foundry            │◄──────(10.2.0.16~18)───│               │ │
│  │ aif-contoso-krc-01    │    account / openai     │  AKS Private  │ │
│  │ (Public Access OFF)   │                         │  Cluster      │ │
│  │ Model: gpt-5.2-chat   │    Private Endpoint     │               │ │
│  └──────────────────────┘    ┌──(10.2.0.20)───────│  aks-contoso  │ │
│  ┌──────────────────────┐    │  registry            │  -krc-01      │ │
│  │ ACR (Premium)         │◄──┘                     │               │ │
│  │ acrcontosokrc01       │                         │  Workload     │ │
│  │ (Public Access OFF)   │                         │  Identity ON  │ │
│  └──────────────────────┘                          │  OIDC Issuer  │ │
│  ┌──────────────────────┐                          │               │ │
│  │ Azure Monitor (AMPLS) │◄──Private Endpoint──────│               │ │
│  │ Log Analytics + AMW   │   (10.2.0.x)            └───────┬───────┘ │
│  └──────────────────────┘                                  │         │
│  ┌──────────────────────┐                                  │         │
│  │ Azure Firewall        │◄──── DNS Proxy (10.100.0.4) ────┘         │
│  │ + TLS Inspection      │                                           │
│  └──────────────────────┘                                           │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. 네트워크

### 2.1 VNet 구성 (Hub-Spoke)

| 구성 요소 | 리소스 | CIDR | 역할 |
|---|---|---|---|
| **Spoke VNet** | `vnet-contoso-koreacentral-01` | `10.0.0.0/10` | AKS + PE Subnet |
| **Hub VNet** | `vnet-contoso-koreacentral-prd-01` | `10.100.0.0/16` | Azure Firewall, DNS Proxy |
| **VNet Peering** | 양방향 Connected | — | Hub-Spoke 연결 |

| Subnet | CIDR | 용도 |
|---|---|---|
| `aks-subnet` | `10.1.0.0/24` | AKS 노드 |
| `pe-subnet` | `10.2.0.0/22` | Private Endpoint |
| `AzureFirewallSubnet` | `10.100.0.0/26` | Azure Firewall |

### 2.2 Azure Firewall & DNS

```
AKS Pod / VM
    │ DNS Query
    ▼
Azure Firewall DNS Proxy (10.100.0.4)
    │ Forward
    ▼
Azure DNS (168.63.129.16)
    │ Private DNS Zone (Hub VNet에 링크)
    ▼
Private IP 반환 (10.2.0.x)
```

- **양쪽 VNet 모두** Custom DNS: `10.100.0.4`
- **Firewall Rule**: `Net-Allow-DNS` — UDP/TCP 53, Source: `10.0.0.0/10`, `10.100.0.0/16`
- **Firewall Rule**: `pypi-and-cdn` — pypi.org, files.pythonhosted.org, cloudflarestorage.com 허용 (이미지 빌드용)
- **TLS Inspection** 활성화 ← AMPLS/Private Link가 필요한 이유

---

## 3. Private Endpoints & DNS Zones

### 3.1 Private Endpoints

모든 PaaS는 공용 액세스 비활성화, Private Endpoint로만 접근:

| Private Endpoint | 대상 서비스 | Group | IP |
|---|---|---|---|
| `pe-aif-contoso-krc-01` | AI Foundry (AIServices) | account | 10.2.0.16 |
| `pe-acr-contoso-krc-01` | ACR (Premium) | registry | 10.2.0.20 |
| `pe-ampls-contoso-krc-01` | Azure Monitor (AMPLS) | azuremonitor | 10.2.0.x |

### 3.2 Private DNS Zones

AI Foundry는 단일 리소스이지만 3개의 DNS Zone이 필요:

| DNS Zone | A Record | 용도 |
|---|---|---|
| `privatelink.cognitiveservices.azure.com` | 10.2.0.16 | Cognitive Services API |
| `privatelink.openai.azure.com` | 10.2.0.17 | OpenAI API (Service Connector 사용) |
| `privatelink.services.ai.azure.com` | 10.2.0.18 | AI Foundry Portal |
| `privatelink.azurecr.io` | 10.2.0.20 / .19 | ACR (registry + data) |
| `privatelink.monitor.azure.com` | 10.2.0.x | Azure Monitor 메트릭 수집 |
| `privatelink.ods.opinsights.azure.com` | 10.2.0.x | ODS 데이터 채널 |
| `privatelink.oms.opinsights.azure.com` | 10.2.0.x | OMS 포털 |
| `privatelink.agentsvc.azure-automation.net` | 10.2.0.x | 에이전트 서비스 |
| `privatelink.blob.core.windows.net` | 10.2.0.x | Blob 스토리지 |

> **모든 DNS Zone은 Hub VNet** (`vnet-contoso-koreacentral-prd-01`)에 링크. Firewall DNS Proxy가 Hub VNet에 있으므로 Azure DNS가 Private IP를 반환하려면 같은 VNet에 링크되어야 함.

---

## 4. Identity & 인증

### 4.1 UAMI (User Assigned Managed Identity)

| 항목 | 값 |
|---|---|
| **이름** | `uami-aif-contoso-krc-01` |
| **Client ID** | `0429ea37-bae6-45c3-beeb-e5efdcaa67ba` |
| **RBAC** | `Cognitive Services OpenAI User` (AI Foundry 스코프) |

### 4.2 Service Connector

> Service Connector는 AKS ↔ Azure PaaS 연결을 자동화하는 서비스. Workload Identity에 필요한 리소스를 모두 자동 생성한다.

| 항목 | 값 |
|---|---|
| **Connector** | `myapp_aif_connection` |
| **대상** | AI Foundry (`aif-contoso-krc-01`) |
| **인증** | `userAssignedIdentity` (Workload Identity) |
| **Namespace** | `myapp` |

#### 자동 생성되는 리소스

```
Service Connector 생성 시 자동 배포:

1. ServiceAccount
   sc-account-0429ea37-bae6-45c3-beeb-e5efdcaa67ba
   → azure.workload.identity/client-id 어노테이션 포함

2. Secret (sc-myappaifconnection-secret)
   ├── AZURE_AISERVICES_CLIENTID
   ├── AZURE_AISERVICES_COGNITIVESERVICES_ENDPOINT
   ├── AZURE_AISERVICES_OPENAI_BASE        ← 앱에서 사용
   └── AZURE_AISERVICES_SPEECH_ENDPOINT

3. Federated Credential (UAMI에 자동 등록)
   sc_acentral01_myapp
   → subject: system:serviceaccount:myapp:sc-account-...
   → issuer: AKS OIDC Issuer URL
```

#### 기존 수동 방식과 비교

| 기존 방식 (수동) | Service Connector |
|---|---|
| SA + WI 어노테이션 수동 설정 | **자동 생성** |
| Federated Credential 수동 등록 | **자동 등록** |
| Endpoint URL을 ConfigMap/Secret에 수동 입력 | **자동 주입** |
| 네임스페이스별 반복 작업 | `--customized-keys`로 유연 구성 |

---

## 5. AKS 클러스터

| 항목 | 값 |
|---|---|
| **이름** | `aks-contoso-koreacentral-01` |
| **유형** | Private Cluster |
| **Workload Identity** | Enabled |
| **OIDC Issuer** | Enabled |
| **Node Taint** | `workload=general:NoSchedule`, `CriticalAddonsOnly=true:NoSchedule` |
| **DNS** | Azure Firewall DNS Proxy (10.100.0.4) |

### Namespace 구성

| Namespace | 용도 | 주요 리소스 |
|---|---|---|
| `myapp` | AG-UI 앱 | Deployment, Service, SC SA/Secret, Instrumentation CR |
| `opentelemetry-operator-system` | OTel Operator + Collector | Operator, OTelCollector CR, PodMonitor |
| `cert-manager` | TLS 인증서 관리 | cert-manager (OTel Webhook용) |
| `kube-system` | 시스템 | ama-metrics (Managed Prometheus) |

---

## 6. AG-UI 애플리케이션

### 6.1 구성

```
┌─ Deployment: agui-server ──────────────────────────────┐
│  Image: acrcontosokrc01.azurecr.io/myapp/agui-server   │
│  Port: 8080                                            │
│                                                        │
│  SA: sc-account-0429ea37-… (← Service Connector 생성)   │
│  Label: azure.workload.identity/use: "true"            │
│  Annotation: instrumentation.opentelemetry.io/          │
│              inject-python: "true"                      │
│                                                        │
│  ┌─ FastAPI Endpoints ──────────────────────────────┐  │
│  │  /chat/       → 브라우저 채팅 UI (HTML/JS)        │  │
│  │  /api/agent   → AG-UI SSE 스트리밍 엔드포인트      │  │
│  │  /docs        → Swagger UI                       │  │
│  └──────────────────────────────────────────────────┘  │
└───────────────┬────────────────────────────────────────┘
                ▼
┌─ Service: agui-server (ClusterIP, 80 → 8080) ─────────┘
```

### 6.2 기술 스택

| 레이어 | 기술 |
|---|---|
| **프로토콜** | AG-UI — SSE 기반 이벤트 스트리밍 |
| **프레임워크** | Microsoft Agent Framework (`agent-framework-ag-ui`) |
| **LLM Client** | `AzureOpenAIChatClient` + `DefaultAzureCredential` |
| **웹 서버** | FastAPI + Uvicorn |
| **계측** | `opentelemetry-instrumentation-openai-v2` + 커스텀 메트릭 |
| **컨테이너** | Python 3.12-slim, Podman 빌드 |
| **프론트엔드** | 순수 HTML/JS 채팅 UI (빌드 도구 없음) |

### 6.3 코드에서 Service Connector 사용

```python
# server.py — 자격 증명 하드코딩 없음
endpoint = os.environ.get("AZURE_AISERVICES_OPENAI_BASE")  # SC Secret에서 자동 주입
chat_client = AzureOpenAIChatClient(
    credential=DefaultAzureCredential(),  # Workload Identity 자동 인식
    endpoint=endpoint,
    deployment_name="gpt-5.2-chat",
)
```

---

## 7. 데이터 흐름

### 7.1 요청 흐름

```
브라우저 ──HTTP──▶ /chat/ (HTML UI)
                    │
                    │ POST /api/agent (SSE)
                    ▼
              FastAPI (agui-server Pod)
                    │ AzureOpenAIChatClient
                    │ credential: Workload Identity Token
                    │ endpoint: SC Secret에서 주입
                    ▼
              AI Foundry (Private Endpoint)
              aif-contoso-krc-01.openai.azure.com → 10.2.0.17
              Model: gpt-5.2-chat
              인증: Entra ID RBAC
```

### 7.2 AG-UI SSE 이벤트 흐름

```
→ POST /api/agent  { messages: [{role:"user", content:"안녕"}] }

← data: {"type":"RUN_STARTED","threadId":"...","runId":"..."}
← data: {"type":"TEXT_MESSAGE_START","messageId":"...","role":"assistant"}
← data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"...","delta":"안녕"}
← data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"...","delta":"하세요!"}
← data: {"type":"TEXT_MESSAGE_END","messageId":"..."}
← data: {"type":"RUN_FINISHED","threadId":"...","runId":"..."}
```

### 7.3 메트릭 흐름

```
agui-server Pod
    │ OTLP (auto-instrumentation)
    ▼
OTel Collector (OTLP :4318 → batch → prometheus :8889)
    │ PodMonitor 스크래핑 (30s)
    ▼
ama-metrics (kube-system)
    │ Private Link (AMPLS)
    ▼
Azure Managed Prometheus → Grafana
```

> 메트릭 파이프라인 상세는 [모니터링 구축 가이드](monitoring.md) 참조.

---

## 8. 보안 요약

| 영역 | 적용 사항 |
|---|---|
| **네트워크** | 모든 PaaS에 Private Endpoint, 공용 접근 차단, Azure Firewall 아웃바운드 제어 |
| **인증** | Workload Identity (Service Connector 자동 구성), RBAC 최소 권한 |
| **비밀 관리** | 코드에 자격 증명 없음 — SC Secret + DefaultAzureCredential |
| **DNS** | Firewall DNS Proxy 경유, Private DNS Zone으로 내부 해석 |
| **컨테이너** | ACR Premium (Private Endpoint), 공용 Pull 없음 |
| **모니터링** | AMPLS 경유 Azure Monitor, OTel Operator 자동 계측 |
