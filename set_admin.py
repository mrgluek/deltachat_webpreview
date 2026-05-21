#!/usr/bin/env python3
import sys
import database

def main():
    print("=== Deltachat WebPreview Admin Configuration ===")
    
    current_email = database.get_config("admin_dc_email")
    current_fp = database.get_admin_fingerprint()
    
    print(f"Current Admin Email: {current_email or 'Not set'}")
    print(f"Current Admin Fingerprint: {current_fp or 'Not set'}")
    print("\nLeave blank to keep current value, type 'clear' to remove.")
    
    new_email = input("New Admin Email: ").strip()
    if new_email:
        if new_email.lower() == 'clear':
            database.set_config("admin_dc_email", "")
        else:
            database.set_config("admin_dc_email", new_email)
            
    new_fp = input("New Admin Fingerprint (e.g. 5A3B...): ").strip()
    if new_fp:
        if new_fp.lower() == 'clear':
            database.set_admin_fingerprint("")
        else:
            # clean up spaces/colons just in case
            cleaned_fp = new_fp.upper().replace(" ", "").replace(":", "")
            database.set_admin_fingerprint(cleaned_fp)
            
    print("\n✅ Admin configuration updated successfully.")
    
if __name__ == "__main__":
    main()
