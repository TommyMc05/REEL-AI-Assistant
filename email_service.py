import os
import resend


def send_email(business_name, to_email, name, contact, issue, description, address, urgency, preferred_time):
    resend.api_key = os.getenv("RESEND_API_KEY")
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

    if not to_email:
        print("No recipient email set for this business.")
        print(body)
        return

    try:
        resend.Emails.send({
            "from": os.getenv("FROM_EMAIL"),
            "to": [to_email],
            "subject": subject,
            "text": body
        })
    except Exception as e:
        print(f"Email send failed: {e}")
        print(body)
