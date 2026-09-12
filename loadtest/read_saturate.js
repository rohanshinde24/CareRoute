import http from "k6/http";
import { check } from "k6";

// No sleep: each VU issues requests back to back, so VUs approximate
// in-flight concurrency rather than think-time-shaped users.
export const options = {
  stages: [
    { duration: "45s", target: 50 },
    { duration: "45s", target: 100 },
    { duration: "45s", target: 200 },
    { duration: "45s", target: 400 },
    { duration: "30s", target: 0 },
  ],
  thresholds: {
    http_req_failed: ["rate<0.01"],
    http_req_duration: ["p(95)<500"],
  },
};

export default function () {
  const response = http.get(`${__ENV.BASE_URL}/api/referrals`);
  check(response, { "HTTP 200": (r) => r.status === 200 });
}
