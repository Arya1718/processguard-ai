import { check, sleep } from 'k6';
import http from 'k6/http';
import { Trend, Counter, Rate } from 'k6/metrics';

const incidentListLatency = new Trend('incident_list_latency', true);
const incidentListErrors = new Counter('incident_list_errors');
const incidentListRate = new Rate('incident_list_success_rate');

const BASE_URL = __ENV.MIDDLEWARE_URL || 'http://localhost:8080';
const BEARER_TOKEN = __ENV.TEST_BEARER_TOKEN || 'test-token';

export const options = {
  stages: [
    { duration: '30s', target: 10 },  // warm up
    { duration: '3m',  target: 10 },  // 10 concurrent dashboard readers
    { duration: '30s', target: 30 },  // spike to 30 (simulating shift change)
    { duration: '3m',  target: 30 },  // sustained spike
    { duration: '30s', target: 0 },   // ramp down
  ],
  thresholds: {
    'incident_list_latency': ['p(95)<300', 'p(99)<800'],
    'incident_list_success_rate': ['rate>0.99'],
    'incident_list_errors': ['count<10'],
    'http_req_duration': ['p(95)<300'],
  },
  tags: { test_run: 'prompt10_dashboard_reads', target_component: 'incident_list_endpoint' },
};

export default function () {
  const params = {
    headers: {
      'Authorization': `Bearer ${BEARER_TOKEN}`,
      'Content-Type': 'application/json',
    },
  };

  // 1. Main dashboard incident list (paginated)
  const listRes = http.get(`${BASE_URL}/api/v1/incidents?limit=50&offset=0`, params);
  const listOk = check(listRes, {
    'list status is 200': (r) => r.status === 200,
    'list has items array': (r) => {
      try {
        const body = JSON.parse(r.body);
        return Array.isArray(body.items) && body.total !== undefined;
      } catch (e) {
        return false;
      }
    },
  });
  incidentListLatency.add(listRes.timings.duration, { endpoint: 'list', success: listOk });
  incidentListRate.add(listOk);
  if (!listOk) incidentListErrors.add(1);

  sleep(0.05);

  // 2. Single incident detail (if any incidents exist)
  let incidentId = null;
  try {
    const body = JSON.parse(listRes.body);
    if (body.items && body.items.length > 0) {
      incidentId = body.items[0].id;
    }
  } catch (e) {
    // parse error -- will skip detail fetch
  }

  if (incidentId) {
    const detailRes = http.get(`${BASE_URL}/api/v1/incidents/${incidentId}`, params);
    check(detailRes, {
      'detail status is 200': (r) => r.status === 200,
      'detail has id': (r) => {
        try {
          return JSON.parse(r.body).id === incidentId;
        } catch (e) {
          return false;
        }
      },
    });
  }

  // 3. Sensor readings endpoint
  const sensorsRes = http.get(`${BASE_URL}/api/v1/sites/11111111-1111-1111-1111-111111111111/sensors/latest`, params);
  check(sensorsRes, {
    'sensors status is 200': (r) => r.status === 200,
    'sensors has sensors array': (r) => {
      try {
        return Array.isArray(JSON.parse(r.body).sensors);
      } catch (e) {
        return false;
      }
    },
  });

  // 4. Health check (should NOT be rate-limited or throttled)
  const healthRes = http.get(`${BASE_URL}/api/v1/health/live`, params);
  check(healthRes, {
    'health status is 200': (r) => r.status === 200,
  });

  sleep(0.2);
}
