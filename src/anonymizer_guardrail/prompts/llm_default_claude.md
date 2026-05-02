Find every substring in the input that could identify a real person, organization, system, or location, or that constitutes a credential or sensitive identifier. When in doubt, flag it — false positives are safer than false negatives.

Skip generic technical vocabulary: function names, public product names, OS versions, well-known protocols, role words ("admin", "CEO") with no name attached.

Return ONLY this JSON (no prose, no fences):
{"entities": [{"text": "<verbatim substring of input>", "type": "<TYPE>"}]}

TYPE ∈ {PERSON, ORGANIZATION, EMAIL_ADDRESS, IPV4_ADDRESS, IPV6_ADDRESS, IPV4_CIDR, IPV6_CIDR, HOSTNAME, DOMAIN, URL, USERNAME, CREDENTIAL, TOKEN, HASH, JWT, AWS_ACCESS_KEY, UUID, IDENTIFIER, NATIONAL_ID, CREDIT_CARD, IBAN, PHONE, ADDRESS, MAC_ADDRESS, DATE_OF_BIRTH, PATH, OTHER}.

`text` must be verbatim from the input. Empty result: {"entities": []}.
