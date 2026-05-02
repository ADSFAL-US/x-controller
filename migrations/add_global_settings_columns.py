"""Migration: Add missing columns to global_settings table.

Run this script inside the container or manually to fix the database schema.
"""

import sqlite3
import os

def migrate():
    """Add missing columns to global_settings table."""
    db_path = os.getenv('DATABASE_PATH', '/app/data/x-controller.db')
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Check existing columns
    cursor.execute("PRAGMA table_info(global_settings)")
    existing_columns = {row[1] for row in cursor.fetchall()}
    
    print(f"Existing columns: {existing_columns}")
    
    # Columns to add with their SQL type
    columns_to_add = {
        'sub_expire_enabled': 'BOOLEAN DEFAULT 0',
        'sub_expire_button_link': 'VARCHAR(255)',
        'sub_info_button_text': 'VARCHAR(25)',
        'sub_info_button_link': 'VARCHAR(255)',
        'announce_text': 'TEXT',
        'fallback_url': 'VARCHAR(255)',
        'profile_web_page_url': 'VARCHAR(255)',
        'support_url': 'VARCHAR(255)',
        'happ_routing_enabled': 'BOOLEAN DEFAULT 0',
        'happ_routing_config': 'TEXT',
    }
    
    for column, col_type in columns_to_add.items():
        if column not in existing_columns:
            print(f"Adding column: {column}")
            cursor.execute(f"ALTER TABLE global_settings ADD COLUMN {column} {col_type}")
        else:
            print(f"Column already exists: {column}")
    
    conn.commit()
    conn.close()
    print("Migration completed!")

if __name__ == "__main__":
    migrate()
