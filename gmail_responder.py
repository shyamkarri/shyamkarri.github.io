import os
import imaplib
import smtplib
import email
from email.header import decode_header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import logging
from datetime import datetime

logger = logging.getLogger("gmail_responder")

# Keywords that indicate a job/recruiter email
JOB_KEYWORDS = [
    "job", "recruiter", "hiring", "interview", "position", "career",
    "role", "apply", "resume", "cv", "contract", "salary", "opportunity",
    "full-time", "part-time", "remote", "w2", "c2c"
]

def clean_header(header_val):
    if not header_val:
        return ""
    decoded = decode_header(header_val)
    parts = []
    for val, encoding in decoded:
        if isinstance(val, bytes):
            try:
                parts.append(val.decode(encoding or "utf-8", errors="ignore"))
            except Exception:
                parts.append(val.decode("utf-8", errors="ignore"))
        else:
            parts.append(str(val))
    return "".join(parts)

def is_job_related(subject, body):
    content = f"{subject} {body}".lower()
    return any(keyword in content for keyword in JOB_KEYWORDS)

def check_and_reply_emails(db, retrieval_chain):
    gmail_user = os.getenv("GMAIL_USER")
    gmail_pass = os.getenv("GMAIL_APP_PASSWORD")

    if not gmail_user or not gmail_pass:
        logger.warning("[Gmail Auto-Responder] Credentials not configured. Skipping.")
        return

    try:
        # 1. Connect to Gmail IMAP
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(gmail_user, gmail_pass)
        mail.select("inbox")

        # Search for all unread emails
        status, messages = mail.search(None, "UNSEEN")
        if status != "OK":
            logger.error("[Gmail Auto-Responder] Failed to search unseen emails.")
            mail.logout()
            return

        email_ids = messages[0].split()
        if not email_ids:
            mail.logout()
            return

        logger.info(f"[Gmail Auto-Responder] Found {len(email_ids)} unread emails.")

        for e_id in email_ids:
            try:
                # Fetch email headers and body
                res, msg_data = mail.fetch(e_id, "(RFC822)")
                if res != "OK":
                    continue

                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])
                        
                        # Extract Headers
                        subject = clean_header(msg["Subject"])
                        from_sender = clean_header(msg["From"])
                        msg_id = msg["Message-ID"]

                        # Extract Body
                        body = ""
                        if msg.is_multipart():
                            for part in msg.walk():
                                content_type = part.get_content_type()
                                content_disposition = str(part.get("Content-Disposition"))
                                if content_type == "text/plain" and "attachment" not in content_disposition:
                                    try:
                                        body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                                    except Exception:
                                        pass
                                    break
                        else:
                            try:
                                body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
                            except Exception:
                                pass

                        # Check if it is a job/recruiter email
                        if is_job_related(subject, body):
                            logger.info(f"[Gmail Auto-Responder] Processing job-related email from: {from_sender}")
                            
                            # Use retrieval_chain to draft professional response
                            query = f"Here is an email I received from a recruiter/job context.\nSender: {from_sender}\nSubject: {subject}\nBody:\n{body}\n\nPlease draft a professional response to this email in first person. Acknowledge the opportunity, answer any specific questions if details exist in context, and offer to coordinate a call."
                            
                            try:
                                response = retrieval_chain.invoke({"input": query, "chat_history": []})
                                reply_text = response.get("answer", "")
                            except Exception as e:
                                logger.error(f"[Gmail Auto-Responder] LangChain invoke error: {e}")
                                continue

                            if reply_text:
                                # Send SMTP Reply
                                send_reply(gmail_user, gmail_pass, from_sender, subject, msg_id, reply_text)
                                
                                # Log to database as Notification
                                from database import Notification
                                notif = Notification(
                                    type="recruiter_email",
                                    title=f"Auto-replied to recruiter: {subject}",
                                    message=f"Sent auto-reply to {from_sender}.\n\nResponse:\n{reply_text}",
                                    data={"sender": from_sender, "original_subject": subject}
                                )
                                db.add(notif)
                                db.commit()
                        else:
                            logger.info(f"[Gmail Auto-Responder] Skipped non-job email: {subject} from {from_sender}")

                        # Mark email as read
                        mail.store(e_id, "+FLAGS", "\\Seen")

            except Exception as ex:
                logger.error(f"[Gmail Auto-Responder] Error processing email ID {e_id}: {ex}")

        mail.logout()
    except Exception as e:
        logger.error(f"[Gmail Auto-Responder] IMAP connection/auth error: {e}")

def send_reply(gmail_user, gmail_pass, to_addr, original_subject, original_msg_id, reply_body):
    try:
        # Format the subject
        subject = original_subject
        if not subject.lower().startswith("re:"):
            subject = "Re: " + subject

        # Create MIME Message
        msg = MIMEMultipart()
        msg["From"] = gmail_user
        msg["To"] = to_addr
        msg["Subject"] = subject

        # Thread headers
        if original_msg_id:
            msg["In-Reply-To"] = original_msg_id
            msg["References"] = original_msg_id

        msg.attach(MIMEText(reply_body, "plain"))

        # Connect and send via SMTP
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, to_addr, msg.as_string())

        logger.info(f"[Gmail Auto-Responder] Successfully sent auto-reply to {to_addr}")
    except Exception as e:
        logger.error(f"[Gmail Auto-Responder] Failed to send email via SMTP: {e}")
