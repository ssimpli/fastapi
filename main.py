import json
import os
import requests
import math
import time  # 시간 지연을 위해 추가
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
LUNCH_START = 720
LUNCH_DURATION = 60

LOADING_TIME = 20
UNLOADING_TIME = 30

# API 호출 간격 (초 단위) - 0.1초 쉼
API_CALL_INTERVAL = 0.1 

NAVER_ID = os.environ.get("NAVER_CLIENT_ID") or os.environ.get("x-ncp-apigw-api-key-id")
NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET") or os.environ.get("x-ncp-apigw-api-key")

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
# 3. 데이터 로드
# ==========================================
NODE_INFO = {}
MATRIX_DATA = {}
PATH_CACHE = {}

def load_data():
    global NODE_INFO, MATRIX_DATA
    raw_data = None
    
    # 1. URL 다운로드
    url = os.environ.get("JEJU_MATRIX_URL")
    if url:
        try:
            print(f"🌐 데이터 다운로드 중... ({url[:30]}...)")
            res = requests.get(url, timeout=10)
            if res.status_code == 200: raw_data = res.json()
        except: pass
    
    # 2. 파일 로드
    if not raw_data and os.path.exists("jeju_distance_matrix_full.json"):
        try:
            with open("jeju_distance_matrix_full.json", "r", encoding="utf-8") as f:
                raw_data = json.load(f)
        except: pass

    if raw_data:
        if isinstance(raw_data, list) and len(raw_data) > 0:
            raw_data = raw_data[0]
            
        if "node_info" in raw_data:
            for node in raw_data["node_info"]:
                NODE_INFO[node["name"]] = {"lat": node["lat"], "lon": node["lon"]}
        
        if "matrix" in raw_data:
            MATRIX_DATA = raw_data["matrix"]
        else:
            MATRIX_DATA = raw_data
            
        print(f"✅ 데이터 로드 완료: 좌표 {len(NODE_INFO)}개")
    else:
        print("⚠️ [경고] 데이터 로드 실패. 기본값 사용.")

load_data()

# ==========================================
# 4. 핵심 함수 (API 호출 제어)
# ==========================================

# [계산용] 절대 API 호출 안 함 (파일 or 수학공식)
def get_driving_time_fast(start_name, end_name):
    # 1. 매트릭스 파일 우선
    if start_name in MATRIX_DATA and end_name in MATRIX_DATA[start_name]:
        try:
            val = float(MATRIX_DATA[start_name][end_name])
            # km단위(2.0 미만)면 분으로 환산, 아니면 그대로 사용
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

# [시각화용] 상세 경로 - 여기서 API 호출 (Throttling 적용)
def get_path_geometry(start_name, end_name):
    key = f"{start_name}->{end_name}"
    if key in PATH_CACHE: return PATH_CACHE[key]
    
    if not NAVER_ID or not NAVER_SECRET: return []
    if start_name not in NODE_INFO or end_name not in NODE_INFO: return []

    # ★ 중요: API 호출 전 강제 휴식 (서버 부하 방지)
    time.sleep(API_CALL_INTERVAL)

    try:
        start = NODE_INFO[start_name]
        goal = NODE_INFO[end_name]
        url = "https://maps.apigw.ntruss.com/map-direction/v1/driving"
        headers = {
            "x-ncp-apigw-api-key-id": NAVER_ID,
            "x-ncp-apigw-api-key": NAVER_SECRET
        }
        params = {
            "start": f"{start['lon']},{start['lat']}",
            "goal": f"{goal['lon']},{goal['lat']}",
            "option": "trafast"
        }
        # 타임아웃 3초 (오래 걸리면 바로 포기하고 다음으로)
        res = requests.get(url, headers=headers, params=params, timeout=3)
        
        if res.status_code == 200:
            json_res = res.json()
            if json_res["code"] == 0:
                path = json_res["route"]["trafast"][0]["path"]
                PATH_CACHE[key] = path
                return path
    except Exception as e:
        print(f"API Error ({start_name}->{end_name}): {e}")
    
    return []

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
        
        # 점심시간 로직
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
    
    # 1. 거리 매트릭스: API 절대 안 씀 (get_driving_time_fast 사용)
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
            geometry = []

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

                next_index = solution.Value(routing.NextVar(index))
                if not routing.IsEnd(next_index):
                    next_node_idx = manager.IndexToNode(next_index)
                    
                    # 2. 결과 생성 시에만 API 호출 (Throttling 적용)
                    seg_path = get_path_geometry(node_name, locs[next_node_idx])
                    if seg_path: geometry.extend(seg_path)
                
                index = next_index

            node_idx = manager.IndexToNode(index)
            end_time = solution.Min(time_dim.CumulVar(index))
            depot_coord = NODE_INFO.get(depot, {"lat": 0, "lon": 0})
            
            last_loc = path[-1]["location"]
            ret_path = get_path_geometry(last_loc, depot)
            if ret_path: geometry.extend(ret_path)
            
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
                    "path": path,
                    "geometry": geometry
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
