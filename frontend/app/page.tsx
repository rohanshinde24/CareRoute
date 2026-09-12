import { ReferralForm } from "./referral-form";

type Referral = { id: string; patient_id: string; requested_specialty: string; reason: string; location_preference: string | null; state: string; created_at: string };
const serverApiUrl = process.env.API_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const publicApiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type ReferralPage = { items: Referral[]; next_cursor: string | null };

async function referrals(): Promise<Referral[]> {
  try {
    const response = await fetch(`${serverApiUrl}/api/referrals?limit=50`, { cache: "no-store" });
    if (!response.ok) return [];
    const page: ReferralPage = await response.json();
    return page.items ?? [];
  } catch {
    return [];
  }
}

export default async function Home() {
  const items = await referrals();
  return <main>
    <header><div className="mark">CR</div><div><p className="eyebrow">Referral operations</p><h1>CareRoute</h1></div><span className="demo">Synthetic data only</span></header>
    <section className="hero"><div><p className="eyebrow">Coordination workspace</p><h2>Move every referral forward.</h2><p>Track administrative referral progress, surface missing information, and coordinate safe scheduling from one calm workspace.</p></div><div className="stat"><strong>{items.length}</strong><span>Referrals on this page</span></div></section>
    <div className="grid"><section className="panel"><div className="panelTitle"><div><p className="eyebrow">Active queue</p><h3>Referrals</h3></div><span>{items.length} shown</span></div>
      {items.length === 0 ? <div className="empty"><span>◎</span><h4>No referrals yet</h4><p>Seed the database or create a synthetic referral to begin.</p></div> : <div className="list">{items.map(item => <article key={item.id}><div><strong>{item.requested_specialty}</strong><p>{item.reason}</p></div><div><span className={`state ${item.state.toLowerCase()}`}>{item.state.replaceAll("_", " ")}</span><time>{new Date(item.created_at).toLocaleDateString()}</time></div></article>)}</div>}
    </section><ReferralForm apiUrl={publicApiUrl} /></div>
  </main>;
}
