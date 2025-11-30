import json
import os
import requests
import math
from typing import List, Dict, Any
from fastapi import FastAPI
from pydantic import BaseModel, model_validator

try:
    from ortools.constraint_solver import routing_enums_pb2
    from ortools.constraint_solver import pywrapcp
except ImportError:
    print("❌ OR-Tools 설치 필요")

app = FastAPI()

# ==========================================
# 1. 설정
# ==========================================
DRIVER_START_TIME = 420  # 07:00
WORK_END_TIME = 1080     # 18:00
LUNCH_START = 720        # 12:00
LUNCH_DURATION = 60      # 1시간

LOADING_TIME = 20
UNLOADING_TIME = 30

# ==========================================
# 2. 데이터 모델
# ==========================================
class OrderItem(BaseModel):
    주유소명: str
    휘발유: int = 0
    등유: int = 0
    경유: int = 0
    start_min: int = 420
    end_min: int = 1080
    priority: int = 2
    
    class Config: extra = 'allow'

    @model_validator(mode='before')
    @classmethod
    def flatten_data(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key in ['주문량', 'order', 'data']:
                if key in data and isinstance(data[key], dict):
                    inner = data[key]
                    if any(k in inner for k in ['휘발유', '경유', '등유']):
                        data.update(inner)
            for field in ['휘발유', '등유', '경유', 'start_min', 'end_min', 'priority']:
                if field in data:
                    try:
                        data[field] = int(data[field]) if data[field] else 0
                    except: data[field] = 0
        return data

class VehicleItem(BaseModel):
    차량번호: str
    유종: str
    수송용량: int

class OptimizationRequest(BaseModel):
    orders: List[OrderItem]
    vehicles: List[VehicleItem]

# ==========================================
# 3. 데이터 로드 (매트릭스 파일만 사용)
# ==========================================
NODE_INFO = {}
MATRIX_DATA = {}

def load_data():
    global NODE_INFO, MATRIX_DATA
    raw_data = None
    
    # 1. URL 다운로드
    url = os.environ.get("JEJU_MATRIX_URL")
    if url:
        try:
            res = requests.get(url, timeout=5)
            if res.status_code == 200: raw_data = res.json()
        except: pass
    
    # 2. 파일 로드
    if not raw_data and os.path.exists("jeju_distance_matrix_full.json"):
        try:
            with open("jeju_distance_matrix_full.json", "r", encoding="utf-8") as f:
                raw_data = json.load(f)
        except: pass

    if raw_data:
        if isinstance(raw_data, list) and len(raw_data) > 0: raw_data = raw_data[0]
        if "node_info" in raw_data:
            for node in raw_data["node_info"]:
                NODE_INFO[node["name"]] = {"lat": node["lat"], "lon": node["lon"]}
        if "matrix" in raw_data: MATRIX_DATA = raw_data["matrix"]
        else: MATRIX_DATA = raw_data
        print(f"✅ 데이터 로드 완료: {len(NODE_INFO)}개 지점")

load_data()

# ==========================================
# 4. 거리 계산 (API 없음, 오직 파일/수학)
# ==========================================
def get_driving_time_fast(start_name, end_name):
    # 1. 매트릭스 파일 사용 (최우선)
    if start_name in MATRIX_DATA and end_name in MATRIX_DATA[start_name]:
        try:
            val = float(MATRIX_DATA[start_name][end_name])
            if val < 2.0: return max(5, int(val * 1.5 * 60)) 
            return int(val)
        except: pass

    # 2. 하버사인 (백업)
    if start_name not in NODE_INFO or end_name not in NODE_INFO: return 20
    
    start = NODE_INFO[start_name]
    goal = NODE_INFO[end_name]
    
    R = 6371
    dLat = math.radians(goal['lat'] - start['lat'])
    dLon = math.radians(goal['lon'] - start['lon'])
    a = math.sin(dLat/2)**2 + math.cos(math.radians(start['lat'])) * math.cos(math.radians(goal['lat'])) * math.sin(dLon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    dist_km = R * c
    
    return max(5, int((dist_km / 40) * 60 * 1.4))

# ==========================================
# 5. OR-Tools 로직
# ==========================================
def solve_multitrip_vrp(all_orders, all_vehicles, fuel_type):
    pending_orders = []
    for o in all_orders:
        amt = o.휘발유 if fuel_type == "휘발유" else (o.등유 + o.경유)
        if amt > 0: pending_orders.append(o)

    my_vehicles = [v for v in all_vehicles if v.유종 == fuel_type]
    
    if not pending_orders or not my_vehicles:
        return {"status": "skipped", "routes": []}

    vehicle_state = {i: DRIVER_START_TIME for i in range(len(my_vehicles))} 
    final_schedule = []
    
    for round_num in range(1, 6):
        if not pending_orders: break
        available_indices = [i for i, t in vehicle_state.items() if t < WORK_END_TIME - 60]
        if not available_indices: break
        
        current_vehicles = [my_vehicles[i] for i in available_indices]
        current_starts = [vehicle_state[i] for i in available_indices]
        
        adjusted_starts = []
        for t in current_starts:
            if t > LUNCH_START - 30 and t < LUNCH_START + LUNCH_DURATION:
                adjusted_starts.append(LUNCH_START + LUNCH_DURATION)
            else:
                adjusted_starts.append(t)
        
        routes, remaining = run_ortools(pending_orders, current_vehicles, adjusted_starts, fuel_type)
        
        if not routes and len(remaining) == len(pending_orders):
            break

        for r in routes:
            real_v_idx = available_indices[r['internal_idx']]
            next_time = r['end_time'] + LOADING_TIME
            if next_time >= LUNCH_START and next_time < LUNCH_START + LUNCH_DURATION:
                next_time = LUNCH_START + LUNCH_DURATION
            
            vehicle_state[real_v_idx] = next_time
            r['round'] = round_num
            r['vehicle_id'] = my_vehicles[real_v_idx].차량번호
            final_schedule.append(r)
            
        pending_orders = remaining

    skipped = [{"name": o.주유소명} for o in pending_orders]
    return {"status": "success", "routes": final_schedule, "unassigned_orders": skipped}

def run_ortools(orders, vehicles, start_times, fuel_type):
    depot = "제주물류센터"
    locs = [depot] + [o.주유소명 for o in orders]
    N = len(locs)
    
    durations = [[0]*N for _ in range(N)]
    for i in range(N):
        for j in range(N):
            if i != j: durations[i][j] = get_driving_time_fast(locs[i], locs[j])

    manager = pywrapcp.RoutingIndexManager(N, len(vehicles), 0)
    routing = pywrapcp.RoutingModel(manager)

    def time_callback(from_i, to_i):
        f, t = manager.IndexToNode(from_i), manager.IndexToNode(to_i)
        service = UNLOADING_TIME if t != 0 else 0
        return durations[f][t] + service

    transit_idx = routing.RegisterTransitCallback(time_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_idx)
    routing.AddDimension(transit_idx, 1440, 1440, False, "Time")
    time_dim = routing.GetDimensionOrDie("Time")
    
    for i in range(len(vehicles)):
        idx = routing.Start(i)
        time_dim.CumulVar(idx).SetMin(int(start_times[i]))

    solver = routing.solver()
    for i in range(len(vehicles)):
        lunch = solver.FixedDurationIntervalVar(LUNCH_START, LUNCH_START, LUNCH_DURATION, False, "Lunch")
        time_dim.SetBreakIntervalsOfVehicle([lunch], i, [])

    time_dim.CumulVar(routing.Start(0)).SetRange(0, 1440)
    for i, order in enumerate(orders):
        index = manager.NodeToIndex(i + 1)
        time_dim.CumulVar(index).SetRange(order.start_min, order.end_min)
        penalty = 100000 if order.priority == 1 else 1000
        routing.AddDisjunction([index], penalty)

    demands = [0] + [ (o.휘발유 if fuel_type=="휘발유" else o.등유+o.경유) for o in orders ]
    def demand_callback(from_i):
        return demands[manager.IndexToNode(from_i)]
    cap_idx = routing.RegisterUnaryTransitCallback(demand_callback)
    routing.AddDimensionWithVehicleCapacity(cap_idx, 0, [v.수송용량 for v in vehicles], True, "Capacity")

    search_params = pywrapcp.DefaultRoutingSearchParameters()
    search_params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search_params.time_limit.seconds = 5
    solution = routing.SolveWithParameters(search_params)
    
    routes = []
    fulfilled_indices = set()
    
    if solution:
        for v_idx in range(len(vehicles)):
            index = routing.Start(v_idx)
            path = []
            load = 0

            while not routing.IsEnd(index):
                node_idx = manager.IndexToNode(index)
                if node_idx > 0: fulfilled_indices.add(node_idx - 1)
                
                t_val = solution.Min(time_dim.CumulVar(index))
                node_name = locs[node_idx]
                coord = NODE_INFO.get(node_name, {"lat": 0, "lon": 0})
                
                path.append({
                    "location": node_name,
                    "lat": coord["lat"], "lon": coord["lon"],
                    "time": t_val, "load": demands[node_idx]
                })
                load += demands[node_idx]
                index = solution.Value(routing.NextVar(index))

            # 복귀
            node_idx = manager.IndexToNode(index)
            end_time = solution.Min(time_dim.CumulVar(index))
            depot_coord = NODE_INFO.get(depot, {"lat": 0, "lon": 0})
            
            path.append({
                "location": depot,
                "lat": depot_coord["lat"], "lon": depot_coord["lon"],
                "time": end_time, "load": 0
            })
            
            if len(path) > 2:
                routes.append({
                    "internal_idx": v_idx, 
                    "end_time": end_time, 
                    "total_load": load, 
                    "path": path
                    # ★ geometry 필드는 이제 n8n에서 채웁니다!
                })
                
    remaining = [orders[i] for i in range(len(orders)) if i not in fulfilled_indices]
    return routes, remaining

@app.post("/optimize")
def optimize(req: OptimizationRequest):
    gas = solve_multitrip_vrp(req.orders, req.vehicles, "휘발유")
    diesel = solve_multitrip_vrp(req.orders, req.vehicles, "등경유")
    return {"gasoline": gas, "diesel": diesel}

@app.get("/")
def health():
    return {"status": "ok"}
```

---

### 2단계: n8n 워크플로우 구성 (Sub-workflow 활용)

제안하신 Sub-workflow 아이디어를 그대로 살려서, n8n에서 상세 경로를 완성하는 로직입니다.

**구조:**
1.  **FastAPI 결과 받음** (경로는 있지만 `geometry`는 비어있음)
2.  **Code Node:** 경로(`path`)를 순회하며 `출발지 좌표 -> 도착지 좌표` 쌍을 만듭니다.
3.  **Loop:** 각 쌍에 대해 Sub-workflow (또는 HTTP Request)를 호출해 네이버에서 `path` 배열을 받아옵니다.
4.  **Merge:** 받아온 `path`들을 합쳐서 최종 HTML 지도 생성 데이터로 넘깁니다.

이 부분은 n8n에서 **`9. 지도 생성` 노드 바로 앞**에 끼워 넣어야 합니다.
(단, n8n 클라우드 버전을 쓰고 계시다면 API 호출 횟수가 많아질 수 있으니 주의하세요. 자체 호스팅이면 상관없습니다.)

**핵심:** 선생님이 이미 만드신 Sub-workflow가 `path`(경로 좌표 배열)를 리턴하도록 조금만 손보시면 됩니다. 현재는 `duration`, `distance`만 리턴하고 있거든요.
Sub-workflow의 `Edit Fields` 노드에 다음을 추가하세요:

```javascript
{
  "name": "path_geometry",
  "value": "={{ $json.route.trafast[0].path }}", // trafast 옵션 사용 시
  "type": "array"
}
