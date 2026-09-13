import http from "k6/http";
import { check } from "k6";

// Ramping arrival rate, not VUs: this asks "can the system absorb N requests
// per second", which is a capacity question. A VU ramp with think time only
// measures how many idle users fit, which is not the same thing.
export const options = {
  discardResponseBodies: true,
  scenarios: {
    capacity: {
      executor: "ramping-arrival-rate",
      startRate: 25,
      timeUnit: "1s",
      preAllocatedVUs: 50,
      maxVUs: 600,
      stages: [
        { duration: "30s", target: 50 },
        { duration: "30s", target: 100 },
        { duration: "30s", target: 200 },
        { duration: "30s", target: 400 },
        { duration: "30s", target: 800 },
      ],
    },
  },
  thresholds: {
    http_req_failed: [{ threshold: "rate<0.01", abortOnFail: false }],
    "http_req_duration{expected_response:true}": ["p(95)<500"],
  },
};

export default function () {
  const r = http.get(`${__ENV.BASE_URL}/api/referrals?limit=25`);
  check(r, { "HTTP 200": (x) => x.status === 200 });
}
