"""Generate a synthetic sample PDF for tests + demo recording.
All names, addresses, account numbers are fictional. Run from project root:
    python tests/fixtures/generate_sample_pdf.py
Writes sample_statement.pdf in the same directory.
"""
import os
import fitz

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_statement.pdf")


def main():
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4 portrait

    text = """ACME CAPITAL ADVISORS
Account Statement
Period: Jan 1, 2026 - Mar 31, 2026

Client:        Jane Q. Sample
Address:       742 Evergreen Terrace, Springfield, IL 62704
Phone:         (555) 010-4242
Email:         jane.sample@example.com

Account Holder:    Sample Family Trust
Account Number:    DEMO 12345 67
Custodian:         ACME Trust Services
Branch:            Springfield Branch (Code: 9001)

Portfolio Summary
-----------------
                                Market Value
Cash & Equivalents             $    42,310.12
Equities                       $   158,920.55
Fixed Income                   $    78,432.00
Alternative Investments        $    12,500.00
                               --------------
Total                          $   292,162.67

Transactions
------------
Date         Description                                 Amount
2026-01-15   Deposit from ACME Trust Services         $50,000.00
2026-02-03   Dividend - Acme Index Fund                $  1,234.56
2026-02-28   Withdrawal to Jane Q. Sample             ($  3,000.00)
2026-03-15   Quarterly Management Fee                  ($    422.10)

For account inquiries:
  - Authorized signer: Jane Q. Sample
  - Contact: 742 Evergreen Terrace, Springfield, IL 62704
  - Reference Account: DEMO 12345 67

This is a SAMPLE statement for demonstration purposes only.
All names, addresses, and account numbers are fictional.
"""

    page.insert_text((50, 50), text, fontsize=10, fontname="Courier")
    doc.save(OUT)
    doc.close()
    print(f"Wrote {OUT} ({os.path.getsize(OUT)} bytes)")


if __name__ == "__main__":
    main()
