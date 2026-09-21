import { check, sleep } from 'k6';
import http from 'k6/http';
import { Trend, Counter, Rate } from 'k6/metrics';

// Custom metrics
const sensorIngestionLatency = new Trend('sensor_ingestion_latency', true);
const sensorIngestionErrors = new Counter('sensor_ingestion_errors');
const sensorIngestionRate = new Rate('sensor_ingestion_success_rate');

// Configuration -- points at the .NET middleware (port 8080 in compose)
const BASE_URL = __ENV.MIDDLEWARE_URL || 'http://localhost:8080';
const BEARER_TOKEN = __ENV.TEST_BEARER_TOKEN || 'test-token';

export const options = {
  stages: [
    { duration: '30s', target: 5 },   // warm up 5 VUs
    { duration: '2m',  target: 5 },   // sustain 5 VUs
    { duration: '30s', target: 20 },  // ramp up to 20 VUs
    { duration: '2m',  target: 20 },  // sustain 20 VUs
    { duration: '30s', target: 0 },   // ramp down
  ],
  thresholds: {
    'sensor_ingestion_latency': ['p(95)<500', 'p(99)<1000'],
    'sensor_ingestion_success_rate': ['rate>0.99'],
    'sensor_ingestion_errors': ['count<10'],
    'http_req_duration': ['p(95)<500'],
  },
  tags: { test_run: 'prompt10_sensor_ingestion', target_component: 'sensor_ingestion' },
};

// Canonical site + equipment from config/rbac-policy.json + seed data
const SITE_ID = '11111111-1111-1111-1111-111111111111';
const EQUIPMENT_ID = '33333333-3333-3333-3333-333333333333';

export default function () {
  // Simulate sensor readings from the cooling water pump
  const sensors = [
    { sensor_type: 'temperature',    value: 38.5,  unit: 'degC', normal_min: 29.0, normal_max: 32.0 },
    { sensor_type: 'vibration',       value: 4.6,   unit: 'mm/s', normal_min: 1.0,  normal_max: 2.5 },
    { sensor_type: 'flow_rate',       value: 106.0, unit: 'm3/h', normal_min: 118.0, normal_max: 132.0 },
    { sensor_type: 'ph',              value: 6.8,   unit: 'pH',   normal_min: 7.5,  normal_max: 8.2 },
    { sensor_type: 'conductivity',    value: 1280.0,unit: 'uS/cm', normal_min: 950.0, normal_max: 1100.0 },
  ];

  for (const s of sensors) {
    const payload = JSON.stringify({
      site_id: SITE_ID,
      equipment_id: EQUIPMENT_ID,
      sensor_type: s.sensor_type,
      value: s.value,
      unit: s.unit,
      normal_min: s.normal_min,
      normal_max: s.normal_max,
      observed_at: new Date().toISOString(),
    });

    const params = {
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${BEARER_TOKEN}`,
      },
    };

    const res = http.post(`${BASE_URL}/api/v1/sensors/reading`, payload, params);

    const ok = check(res, {
      'status is 200 or 201': (r) => r.status === 200 || r.status === 201,
      'response has success flag': (r) => {
        try {
          const body = JSON.parse(r.body);
          return body.received === true || body.acknowledged === true;
        } catch (e) {
          return false;
        }
      },
    });

    sensorIngestionLatency.add(res.timings.duration, { success: ok });
    sensorIngestionRate.add(ok);
    if (!ok) {
      sensorIngestionErrors.add(1);
    }

    // Brief pause between sensor posts to simulate realistic cadence
    sleep(0.1);
  }

  // Hold for a moment between cycles
  sleep(1);
}
