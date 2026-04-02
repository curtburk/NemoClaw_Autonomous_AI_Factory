from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import random
import uvicorn

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

districts = ["district_1", "district_2", "district_3", "district_4", "district_5", "district_6"]

def gen_power(district):
    if district == "district_3":
        v = round(random.uniform(90, 98), 1)
        return {"voltage": v, "load_pct": round(random.uniform(30, 85), 1), "outage": False, "status": "warning"}
    v = round(random.uniform(115, 125), 1)
    return {"voltage": v, "load_pct": round(random.uniform(30, 85), 1), "outage": False, "status": "healthy"}

def gen_water(district):
    if district == "district_5":
        p = round(random.uniform(20, 30), 1)
        return {"pressure_psi": p, "flow_rate": round(random.uniform(5, 15), 1), "quality_index": random.randint(85, 100), "status": "critical"}
    p = round(random.uniform(45, 80), 1)
    return {"pressure_psi": p, "flow_rate": round(random.uniform(5, 15), 1), "quality_index": random.randint(85, 100), "status": "healthy"}

def gen_traffic(district):
    if district == "district_1":
        c = round(random.uniform(0.85, 0.95), 2)
        return {"congestion_index": c, "incident_count": random.randint(3, 5), "avg_speed_mph": round(random.uniform(5, 12), 1), "status": "critical"}
    c = round(random.uniform(0.1, 0.5), 2)
    return {"congestion_index": c, "incident_count": random.randint(0, 2), "avg_speed_mph": round(random.uniform(25, 45), 1), "status": "healthy"}

@app.get("/api/health")
def health():
    result = {"districts": {}, "overall": "healthy"}
    for d in districts:
        p = gen_power(d)
        w = gen_water(d)
        t = gen_traffic(d)
        statuses = [p["status"], w["status"], t["status"]]
        result["districts"][d] = {"power": p["status"], "water": w["status"], "traffic": t["status"]}
        if "critical" in statuses:
            result["overall"] = "critical"
        elif "warning" in statuses and result["overall"] != "critical":
            result["overall"] = "warning"
    return result

@app.get("/api/sensors")
def sensors():
    result = {}
    for d in districts:
        result[d] = {"power": gen_power(d), "water": gen_water(d), "traffic": gen_traffic(d)}
    return result

@app.get("/api/anomalies")
def anomalies():
    p3 = gen_power("district_3")
    w5 = gen_water("district_5")
    t1 = gen_traffic("district_1")
    return {"anomalies": [
        {"district": "district_3", "system": "power", "field": "voltage", "current_value": p3["voltage"], "normal_range": "115-125V", "severity": "warning"},
        {"district": "district_5", "system": "water", "field": "pressure_psi", "current_value": w5["pressure_psi"], "normal_range": "45-80 PSI", "severity": "critical"},
        {"district": "district_1", "system": "traffic", "field": "congestion_index", "current_value": t1["congestion_index"], "normal_range": "0.1-0.5", "severity": "critical"},
    ]}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)