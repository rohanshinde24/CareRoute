"use client";
import { FormEvent, useState } from "react";

export function ReferralForm({ apiUrl }: { apiUrl: string }) {
  const [message, setMessage] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const locationPreference = String(form.get("location_preference") ?? "").trim();
    const response = await fetch(`${apiUrl}/api/referrals`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ patient_id: form.get("patient_id"), requested_specialty: form.get("specialty"), reason: form.get("reason"), location_preference: locationPreference || null, is_synthetic: true }) });
    setMessage(response.ok ? "Referral created. Refresh to see it in the queue." : "Could not create referral. Check the patient ID and fields.");
    if (response.ok) event.currentTarget.reset();
  }
  return <section className="panel formPanel"><p className="eyebrow">New intake</p><h3>Create referral</h3><p className="hint">Administrative coordination for synthetic patients.</p><form onSubmit={submit}><label>Patient ID<input name="patient_id" type="text" placeholder="UUID from synthetic seed" required /></label><label>Requested specialty<input name="specialty" type="text" placeholder="e.g. Cardiology" minLength={2} maxLength={120} pattern="[A-Za-z][A-Za-z0-9 .&/()'\-]*" title="Enter a specialty name such as Cardiology" required /></label><label>Location preference (optional)<input name="location_preference" type="text" placeholder="e.g. San Francisco" minLength={2} maxLength={200} /></label><label>Referral reason<textarea name="reason" placeholder="Administrative referral context" minLength={3} required /></label><button type="submit">Create referral <span>→</span></button>{message && <p className="message" role="status">{message}</p>}</form></section>;
}
