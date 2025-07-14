import http from 'k6/http';
import { check, sleep } from 'k6';

// 1. Define your test configuration
export let options = {
  stages: [
    { duration: '30s', target: 50 },   // ramp up to 50 virtual users
    { duration: '1m',  target: 50 },   // stay at 50 for 1 minute
    { duration: '30s', target: 0 },    // ramp down to 0
  ],
  thresholds: {
    // fail test if >1% of requests are errors
    'http_req_failed': ['rate<0.01'],
    // 95% of requests must finish below 500 ms
    'http_req_duration': ['p(95)<500'],
  },
};

export default function () {
  // 2. Hit your endpoints
  let res = http.get('https://2e14b9e77cd3.ngrok-free.app');
  check(res, {
    'homepage status is 200': (r) => r.status === 200,
  });

  // 3. Optionally hit other critical paths
  res = http.get('https://2e14b9e77cd3.ngrok-free.app');
  check(res, {
    'search status is 200': (r) => r.status === 200,
  });

  // 4. Pause between iterations
  sleep(1);
}
