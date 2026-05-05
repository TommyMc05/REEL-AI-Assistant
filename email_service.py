import os
import resend
from database import save_lead


def send_email(business_id, business_name, to_email, name, contact, issue,
               description, address, urgency, preferred_time):
    resend.api_key = os.getenv("RESEND_API_KEY", "").strip()
    subject = f"New Lead — {business_name}"
    body = f"""New lead received via chat widget.

Business:       {business_name}
Name:           {name}
Contact:        {contact}
Job Type:       {issue}
Description:    {description}
Address:        {address}
Urgency:        {urgency}
Preferred Time: {preferred_time}
"""

    email_sent = False
    if to_email:
        try:
            resend.Emails.send({
                "from": os.getenv("FROM_EMAIL"),
                "to": [to_email],
                "subject": subject,
                "text": body
            })
            email_sent = True
        except Exception as e:
            print(f"[ERROR] Email send failed: {e}")
            print(body)
    else:
        print("No recipient email set for this business.")
        print(body)

    # Always save the lead, even if the email failed
    try:
        save_lead(
            business_id=business_id,
            business_name=business_name,
            name=name,
            contact=contact,
            address=address,
            issue=issue,
            description=description,
            urgency=urgency,
            preferred_time=preferred_time,
            email_sent=email_sent,
        )
    except Exception as e:
        print(f"[ERROR] Failed to save lead to DB: {e}")
