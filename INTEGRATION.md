# Integrating product apps with the subscription dashboard

This doc describes what **Ghost Job Checker**, **Deadline Extractor**, and other Trusted Tools products need to do to work with the subscription model.

## How it works

- Customers buy (one-time or subscription) via Stripe. The **dashboard** creates a row and generates the **access code**; the dashboard also sends a **welcome email** with the code.
- After checkout, Stripe should redirect to the **dashboard success page** (`/success?session_id={CHECKOUT_SESSION_ID}`), where the user sees their code and a link to the portal.
- Customers can look up and resend codes at the **User Portal** (this app).
- Each product app (e.g. ghostjobs.trusted-tools.com, extractor.trusted-tools.com) must **validate** the short dashboard code via the API and grant access when valid; they do **not** generate or email codes themselves.

## What the dashboard provides

### Access-code validation API

- **URL:** `POST https://<your-dashboard-url>/api/access/validate`
- **Content-Type:** `application/json`
- **Body:**
  ```json
  {
    "access_code": "the-code-the-user-pasted",
    "product_id": "prod_xxxx"
  }
  ```
  - `product_id` is the **Stripe product ID** for that app (e.g. the Ghost Jobs product in Stripe). Get it from your Stripe Dashboard or from env (e.g. `STRIPE_PRODUCT_ID`). If omitted, the API validates the code for any product (less strict).
- **Response (200):**
  - Valid: `{ "valid": true, "active": true }`
  - Invalid: `{ "valid": false }`
- **Response (400):** Missing `access_code` → `{ "valid": false }`

Use HTTPS in production. The endpoint does not require auth; the access code itself is the secret.

### Success URL after checkout

- **URL:** `GET https://<your-dashboard-url>/success?session_id={CHECKOUT_SESSION_ID}`
- When creating a Stripe Checkout session, set **success_url** to this (with `{CHECKOUT_SESSION_ID}` so Stripe substitutes the session id). The dashboard will look up the purchase and show the access code and a link to the portal. If the webhook hasn’t run yet, the page shows “Processing your purchase… refresh in a moment.”

### Welcome email

- When the webhook receives `checkout.session.completed`, the dashboard inserts the subscription and sends one **welcome email** to the customer with the access code and a link to the portal. No need for the product app to send its own “your code” email.

## What each product app needs to do

1. **Redirect to the dashboard after checkout**  
   Set Stripe Checkout `success_url` to `https://<dashboard-url>/success?session_id={CHECKOUT_SESSION_ID}` so the user lands on the dashboard success page and sees their code (and can open the portal).

2. **Know their Stripe product ID**  
   Each app should have its Stripe product ID (e.g. `prod_xxx` for Ghost Jobs, a different one for Extractor) in config/env so they can pass it when validating.

3. **Add a way for users to enter an access code**  
   - e.g. an “Access code” or “License key” field (and/or a link to the subscription portal: “Get your code”).

4. **Call the validation API**  
   - When the user submits the code (and optionally on each protected request or on session load), send `POST` to the dashboard’s `/api/access/validate` with `access_code` and `product_id`.
   - If the response is `valid: true`, grant access (e.g. set session/cookie, show full app).
   - If `valid: false`, show an error or paywall and prompt to enter a valid code or purchase.

5. **Optional: link to the portal**  
   - Add a link like “Manage subscriptions / Get your access code” pointing to the dashboard’s User Portal URL so users can look up their code by email.

## Example (pseudo-code)

```python
# In Ghost Jobs or Extractor backend
import os
import requests

DASHBOARD_URL = os.getenv("SUBSCRIPTION_DASHBOARD_URL", "https://your-dashboard.onrender.com")
STRIPE_PRODUCT_ID = os.getenv("STRIPE_PRODUCT_ID")  # e.g. prod_xxx for Ghost Jobs

def validate_access_code(code: str) -> bool:
    if not code or not STRIPE_PRODUCT_ID:
        return False
    r = requests.post(
        f"{DASHBOARD_URL}/api/access/validate",
        json={"access_code": code.strip(), "product_id": STRIPE_PRODUCT_ID},
        timeout=10,
    )
    if r.status_code != 200:
        return False
    return r.json().get("valid") is True
```

Frontend: user enters code → your backend calls `validate_access_code(code)` → if True, set session and allow access; if False, show error or paywall.

## Summary

| Item | Dashboard (this app) | Ghost Jobs / Extractor / etc. |
|------|----------------------|-------------------------------|
| Store purchases & access codes | ✅ | — |
| User portal (look up by email) | ✅ | Link to it (optional) |
| Validate access code for a product | ✅ `/api/access/validate` | Call this API with code + product_id |
| UI for entering code | — | ✅ Add field + submit |
| Grant access when valid | — | ✅ Session/cookie + gate content |

No change is required to the dashboard beyond deploying it and (for other apps) using the validation API and Stripe product IDs as above.
