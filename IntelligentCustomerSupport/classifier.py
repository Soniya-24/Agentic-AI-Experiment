"""
Classification Engine
Classifies customer queries along three dimensions:
- Intent
- Complexity
- Priority (with Critical/Security rules taking precedence)
"""

def classify_query(query_text: str) -> dict:
    if not query_text or not isinstance(query_text, str):
        return {
            "intent": "General Information",
            "complexity": "Low",
            "priority": "P4"
        }
    
    text = query_text.lower().strip()
    
    # 1. P1 - CRITICAL INCIDENT / SECURITY / FRAUD (Highest Precedence)
    p1_triggers = [
        "security incident", "data breach", "application is down", 
        "all users", "service outage", "unauthorized transaction", 
        "unauthorized swipe", "card stolen", "account takeover", 
        "hacked", "fraudulent", "unauthorized debit", "immediate card block"
    ]
    if any(trigger in text for trigger in p1_triggers):
        return {
            "intent": "Security / Fraud",
            "complexity": "High",
            "priority": "P1"
        }
    
    # 2. TRANSACTION DISPUTE / BILLING / ATM FAILURE (High Priority)
    dispute_triggers = [
        "double charge", "duplicate transaction", "charged twice", 
        "atm cash not dispensed", "money debited", "atm failed", 
        "wrong amount", "dispute charge", "dispute", "billing", 
        "invoice", "payment deduction"
    ]
    if any(trigger in text for trigger in dispute_triggers):
        return {
            "intent": "Transaction Dispute",
            "complexity": "Medium",
            "priority": "P2"
        }
    
    # 3. ACCOUNT & ACCESS / AUTHENTICATION (High/Medium Priority)
    access_triggers = [
        "cannot log in", "account locked", "password", 
        "netbanking", "unlock", "forgot password", "two-factor", "2fa"
    ]
    if any(trigger in text for trigger in access_triggers):
        return {
            "intent": "Account & Access",
            "complexity": "Medium",
            "priority": "P2"
        }
    
    # 4. UNRESOLVED REPEAT ISSUE / PREVIOUS FAILURE ESCALATION
    unresolved_triggers = [
        "still unresolved", "did not work", "not working", 
        "issue persists", "previous support ticket", "previous steps"
    ]
    if any(trigger in text for trigger in unresolved_triggers):
        return {
            "intent": "Unresolved Issue",
            "complexity": "High",
            "priority": "P2"
        }
    
    # 5. TECHNICAL ERROR / APP CRASH
    tech_triggers = [
        "server error", "internal server error", "app crash", 
        "export fails", "glitch", "system failure"
    ]
    if any(trigger in text for trigger in tech_triggers):
        return {
            "intent": "Technical Error",
            "complexity": "High",
            "priority": "P2"
        }

    # 6. KYC & ACCOUNT SERVICING
    kyc_triggers = [
        "kyc", "address update", "aadhaar", "pan card", 
        "re-verification", "document upload", "cheque book"
    ]
    if any(trigger in text for trigger in kyc_triggers):
        return {
            "intent": "KYC & Account Servicing",
            "complexity": "Medium",
            "priority": "P3"
        }

    # 7. FEATURE REQUEST / PRODUCT FEEDBACK
    feature_triggers = [
        "add dark mode", "feature request", "new feature", 
        "feedback", "fingerprint login", "biometric login"
    ]
    if any(trigger in text for trigger in feature_triggers):
        return {
            "intent": "Feature Request",
            "complexity": "Low",
            "priority": "P4"
        }

    # 8. GENERAL BANKING INFORMATION (Fallback default)
    return {
        "intent": "General Information",
        "complexity": "Low",
        "priority": "P3"
    }
