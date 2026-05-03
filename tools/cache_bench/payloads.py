"""Multi-turn conversation generator for the cache benchmark.

Realistic chat shapes with PII recurring across turns — that's what
the cache leverages. Each scenario fixes a `Persona` (name, email,
phone, address, credit card, IBAN, IP, etc.) and walks templated
user/assistant exchanges; the same PII reappears so cross-turn cache
hits are observable.

The conversation length is parameterised (5/10/20/30 turns) but
content cycles through a fixed bank of templates per scenario, so a
30-turn conversation is just longer than a 5-turn one — same starting
shape, more revisits. That keeps cache-hit ratios comparable across
lengths.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Persona:
    """One synthetic user with full PII profile. Templates reference
    these fields by name; same persona keeps the same identity across
    every turn of one conversation."""

    name: str
    email: str
    phone: str
    address: str
    cc_number: str
    iban: str
    national_id: str
    ip_address: str
    domain: str


@dataclass(frozen=True)
class Conversation:
    """One simulated multi-turn dialogue."""

    name: str
    """Human-readable label (e.g. `support-5`, `hr-30`)."""

    user_msgs: list[str] = field(default_factory=list)
    assistant_resps: list[str] = field(default_factory=list)
    """Aligned arrays — `user_msgs[i]` and `assistant_resps[i]`
    represent the i-th turn."""

    @property
    def length(self) -> int:
        return len(self.user_msgs)


# ── Persona bank ──────────────────────────────────────────────────────


_PERSONAS: dict[str, Persona] = {
    "support": Persona(
        name="Alice Johnson",
        email="alice.johnson@acmecorp.com",
        phone="+1-555-234-5678",
        address="123 Main St, Springfield, IL 62701",
        cc_number="4532-1234-5678-9012",
        iban="DE89-3704-0044-0532-0130-00",
        national_id="123-45-6789",
        ip_address="192.168.42.17",
        domain="acmecorp.com",
    ),
    "hr": Persona(
        name="Bob Smith",
        email="bob.smith@example.org",
        phone="+1-555-345-6789",
        address="456 Oak Ave, Portland, OR 97201",
        cc_number="5454-9876-5432-1098",
        iban="GB29-NWBK-6016-1331-9268-19",
        national_id="234-56-7890",
        ip_address="10.20.30.40",
        domain="example.org",
    ),
    "tech": Persona(
        name="Carol Davis",
        email="carol.davis@techfirm.io",
        phone="+1-555-456-7890",
        address="789 Pine Rd, Austin, TX 78701",
        cc_number="3412-345678-90123",
        iban="FR14-2004-1010-0505-0001-3M02-606",
        national_id="345-67-8901",
        ip_address="172.16.5.99",
        domain="techfirm.io",
    ),
}


# ── Per-scenario templates ────────────────────────────────────────────
# 30 distinct turns per scenario so a length-30 conversation has fully
# unique per-turn content (no synthetic prefix that scatters cache
# keys). Beyond turn 30 the templates wrap, but the bench tops out at
# 30 turns so wrap-around doesn't fire.

_USER_TEMPLATES_SUPPORT: list[str] = [
    "Hi, I'm {name} and my email is {email}. I'd like to update my account.",
    "My phone number is {phone}, please use it for the appointment confirmation.",
    "I tried logging in from {ip_address} but kept getting locked out.",
    "Can you charge my card {cc_number} for the renewal?",
    "Please ship the form to {address}. I should be home all week.",
    "For tax purposes, my SSN on file is {national_id}.",
    "I'm using a corporate account at {domain}; should that affect anything?",
    "I'd like to confirm: {email} is the primary contact, {phone} is secondary.",
    "If the IP {ip_address} appears in your logs, that's me.",
    "The new card I'd like to add is {cc_number}. Replace the old one.",
    "Please verify {name} as the account holder.",
    "The address change is from my old address to {address}.",
    "My European wire IBAN is {iban} for the international transfer.",
    "Email correspondence to {email} only; phone is a backup.",
    "I'm calling on behalf of {name}; my number is {phone}.",
    "Please update the billing address to {address} effective today.",
    "Can you send a copy of the agreement to {email}?",
    "The login attempt from {ip_address} was definitely me — don't lock it.",
    "For verification: my last four card digits are 9012. Full card on file is {cc_number}.",
    "Confirming SSN {national_id} for the credit check.",
    "I'm at {address} — please update emergency contact too.",
    "Is the wire to {iban} processed yet? It's been three business days.",
    "My contact details are: {name}, {email}, {phone}.",
    "Logging in from {ip_address} — different network than usual.",
    "Card {cc_number} is the only one authorised for charges over $500.",
    "Please call {phone} between 10am and 4pm Eastern.",
    "Send the receipt to {email} as a PDF.",
    "Confirm the delivery address: {address}, suite 4B.",
    "The corporate account is registered to {domain}.",
    "Updating SSN on file from old to {national_id}.",
]

_ASSISTANT_TEMPLATES_SUPPORT: list[str] = [
    "Hello {name}, I see your account associated with {email}. What change would you like to make?",
    "Confirmed, {phone} is on file. We'll text confirmations there.",
    "I see the lockout from {ip_address}. I'll reset it for {name}.",
    "Card {cc_number} authorised; receipt to {email}.",
    "Form will ship to {address} via standard mail.",
    "SSN {national_id} verified against our records.",
    "Yes — the {domain} corporate account has different SLA terms; let me apply them.",
    "Confirmed: primary {email}, secondary {phone}.",
    "{ip_address} added to the trusted-IPs list for {name}.",
    "New card {cc_number} replaces the prior one. Effective immediately.",
    "Account holder {name} verified.",
    "Old address removed; new address is {address}.",
    "Wire to {iban} initiated — should clear in 2-3 business days.",
    "All correspondence will route to {email}; {phone} is backup only.",
    "Got it — calling on behalf of {name} from {phone}.",
    "Billing address updated to {address}.",
    "Agreement PDF on its way to {email}.",
    "The login from {ip_address} is now whitelisted for {name}.",
    "Card {cc_number} (•9012) confirmed as the active payment method.",
    "Credit check authorised against SSN {national_id}.",
    "Emergency contact and primary address both set to {address}.",
    "Wire to {iban} cleared this morning — confirmation email to {email}.",
    "Contact info on file: {name} / {email} / {phone}.",
    "Login from {ip_address} flagged as a new device — sending verification to {email}.",
    "{cc_number} is now the only card authorised for high-value charges.",
    "I'll have an agent call {phone} this afternoon, {name}.",
    "Receipt PDF queued for {email}.",
    "Delivery confirmed: {address}, suite 4B.",
    "{domain} corporate plan attached to your account.",
    "SSN updated from prior value to {national_id}.",
]

_USER_TEMPLATES_HR: list[str] = [
    "Hi, I'm {name} requesting time off. My work email is {email}.",
    "My direct number for urgent matters is {phone}.",
    "I'll be working remotely from {address} during the time off.",
    "Please direct-deposit the next paycheck to my IBAN {iban}.",
    "For the visa application I need a letter referencing my SSN {national_id}.",
    "I'm logging in from {ip_address} on personal Wi-Fi — not the office.",
    "The corporate domain is {domain}; my alias is {email}.",
    "Travel reimbursement: charge my corporate card {cc_number}.",
    "Personal phone: {phone}. Use it only outside business hours.",
    "Forwarding address while away: {address}.",
    "Please CC the manager when emailing {email}.",
    "The IT incident from {ip_address} is on my report — that's a known device.",
    "My supervisor needs to verify {name} for the badge renewal.",
    "Send the printed forms to {address}, attention HR.",
    "If charging anything, use {cc_number} — the limit is $5k.",
    "My SSN is {national_id}; please attach it to the visa packet.",
    "I'm reachable at {phone} for the next two weeks.",
    "Wire any final payments to {iban}.",
    "All HR correspondence to {email}, please.",
    "The shared drive at {domain} has my updated CV.",
    "Logging in from {ip_address} for the policy review.",
    "Confirming account holder is {name}.",
    "I prefer {phone} over the desk extension.",
    "Personal address for tax forms: {address}.",
    "Payroll IBAN is {iban} — same as the prior cycle.",
    "Final SSN-on-file confirmation: {national_id}.",
    "Card {cc_number} for travel; please raise the temp limit.",
    "Domain alias {email} forwards to my personal box.",
    "Office IP {ip_address} should be allow-listed for VPN.",
    "Final answer: ship to {address}, attention HR.",
]

_ASSISTANT_TEMPLATES_HR: list[str] = [
    "Got it, {name}. I'll reach you at {email} for the time-off paperwork.",
    "Direct line {phone} added to the urgent-contact list.",
    "Remote-work address {address} logged for the period.",
    "Direct deposit set to IBAN {iban}.",
    "Visa letter draft will reference SSN {national_id}.",
    "{ip_address} marked as personal Wi-Fi — not flagged as anomaly.",
    "{domain} alias {email} confirmed.",
    "Corporate card {cc_number} cleared for travel reimbursement.",
    "Personal phone {phone} flagged as out-of-hours-only.",
    "Forwarding address {address} on file for the period.",
    "Manager will be CC'd on emails to {email}.",
    "{ip_address} is a known device; the IT report is updated.",
    "Badge renewal authorised for {name}.",
    "Forms will go to {address}, attention HR.",
    "Card {cc_number} authorised; temp limit raised to $5k.",
    "SSN {national_id} attached to the visa packet.",
    "Phone {phone} added as primary for the next two weeks.",
    "Final payment will wire to {iban}.",
    "HR correspondence will route to {email} only.",
    "Updated CV at the {domain} share confirmed.",
    "{ip_address} access for the policy review approved.",
    "Account holder {name} verified.",
    "Direct line preference noted: {phone}.",
    "Tax forms will use the personal address {address}.",
    "Payroll IBAN {iban} matches the prior cycle.",
    "SSN {national_id} on file confirmed.",
    "Travel card {cc_number} temp limit raised to $5k.",
    "{email} alias on {domain} forwards as you requested.",
    "Office IP {ip_address} allow-listed for VPN.",
    "Shipment to {address} attention HR queued.",
]

_USER_TEMPLATES_TECH: list[str] = [
    "Hi I'm {name}; my account uses {email}. My API is failing for users at {domain}.",
    "Logs show errors from server {ip_address}. Anything I can do?",
    "Reach me at {phone} if you need to debug live.",
    "Please charge my card {cc_number} for the upgrade.",
    "Office address for the contract: {address}.",
    "For tax purposes I need an invoice referencing SSN {national_id}.",
    "Migrate my domain {domain} to your CDN — same email {email}.",
    "I'm seeing 502s from {ip_address} routed through the load balancer.",
    "Subscription renewal: card {cc_number}, billing to {email}.",
    "Mail the SLA hardcopy to {address}.",
    "Wire the refund to my IBAN {iban}.",
    "I'd like a callback on {phone} after support hours.",
    "Confirm account holder {name} for the ticket escalation.",
    "Domain {domain} verification email should go to {email}.",
    "Card {cc_number} is the only one authorised for renewals.",
    "Trace shows the issue starts at {ip_address}.",
    "SSN {national_id} for the W-9 form.",
    "Update the contact list: {name} primary, {phone} secondary.",
    "Office relocation: {address}.",
    "Logged in from {ip_address}; might be a new device.",
    "Add SSL for {domain} on the same plan.",
    "Refund please — wire to IBAN {iban}.",
    "Reach me on {phone} between 9am and 5pm Pacific.",
    "Billing details: {name}, {email}, {address}.",
    "Renewal card {cc_number} expires next year.",
    "The new IP {ip_address} should be added to firewall rules.",
    "SSN {national_id} for the form.",
    "Email all support tickets to {email}; CC {phone} owner.",
    "Domain on file is {domain}.",
    "Final shipping address: {address}.",
]

_ASSISTANT_TEMPLATES_TECH: list[str] = [
    "Hello {name}. I see {email} on the account — let me look at the {domain} traffic.",
    "Logs from {ip_address} show a TLS handshake error; I'll dig in.",
    "I'll call {phone} if I need a live debug.",
    "Card {cc_number} authorised; upgrade applied.",
    "SLA contract will ship to {address}.",
    "Invoice referencing SSN {national_id} queued.",
    "Domain {domain} migration started; verification to {email}.",
    "{ip_address} traffic confirmed routing through the LB; I'll repath.",
    "Renewal on {cc_number} processed; receipt to {email}.",
    "Hardcopy SLA to {address} — should arrive within a week.",
    "Refund to IBAN {iban} queued.",
    "I'll call {phone} this evening, {name}.",
    "Account holder {name} verified — escalating.",
    "Verification mail to {email} for {domain}.",
    "Card {cc_number} flagged as the only renewal-authorised card.",
    "Investigating from {ip_address} downward.",
    "SSN {national_id} attached to the W-9.",
    "{name} set as primary; {phone} as secondary.",
    "Office address updated to {address}.",
    "{ip_address} flagged as new — verification sent to {email}.",
    "SSL added for {domain}.",
    "Refund wired to IBAN {iban}.",
    "I'll keep calls to {phone} within 9am-5pm Pacific.",
    "Billing: {name} / {email} / {address} confirmed.",
    "Card {cc_number} expiry on file noted.",
    "Firewall rule added for {ip_address}.",
    "SSN {national_id} attached to the form.",
    "Tickets routing to {email}; {phone} owner CC'd.",
    "Account-domain on file: {domain}.",
    "Shipping to {address} confirmed.",
]


_TEMPLATES: dict[str, tuple[list[str], list[str]]] = {
    "support": (_USER_TEMPLATES_SUPPORT, _ASSISTANT_TEMPLATES_SUPPORT),
    "hr":      (_USER_TEMPLATES_HR,      _ASSISTANT_TEMPLATES_HR),
    "tech":    (_USER_TEMPLATES_TECH,    _ASSISTANT_TEMPLATES_TECH),
}


# ── Public API ────────────────────────────────────────────────────────


def build_conversation(scenario: str, length: int) -> Conversation:
    """Construct one conversation. `scenario` selects the persona +
    template bank; `length` is the number of turns (5/10/20/30
    typical). Same arguments → same content (deterministic for
    reproducible benchmarks)."""
    if scenario not in _TEMPLATES:
        raise ValueError(
            f"Unknown scenario {scenario!r}. "
            f"Available: {', '.join(sorted(_TEMPLATES))}."
        )
    persona = _PERSONAS[scenario]
    user_templates, assistant_templates = _TEMPLATES[scenario]
    fields = persona.__dict__

    user_msgs: list[str] = []
    assistant_resps: list[str] = []
    for i in range(length):
        user_msgs.append(user_templates[i % len(user_templates)].format(**fields))
        assistant_resps.append(
            assistant_templates[i % len(assistant_templates)].format(**fields)
        )
    return Conversation(
        name=f"{scenario}-{length}",
        user_msgs=user_msgs,
        assistant_resps=assistant_resps,
    )


SCENARIOS: tuple[str, ...] = tuple(sorted(_TEMPLATES))
"""Ordered list of available scenario names."""

CONVERSATION_LENGTHS: tuple[int, ...] = (5, 10, 20, 30)
"""Standard conversation lengths the bench iterates by default."""


__all__ = [
    "CONVERSATION_LENGTHS",
    "Conversation",
    "Persona",
    "SCENARIOS",
    "build_conversation",
]
