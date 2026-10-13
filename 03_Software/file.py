import os

def create_project_folders(base_directory="."):
    """
    Creates a standardized folder structure for the PCB/Simulation project.
    """
    # List of folders to create
    folders = [
        "01_Hardware",
        "02_Simulation",
        "03_Software",
        "04_Data",
        "05_Outputs",
        "06_Docs",
        "07_Archive"
    ]

    print(f"Creating folder structure in: {os.path.abspath(base_directory)}\n")

    for folder in folders:
        # Create the full path
        folder_path = os.path.join(base_directory, folder)
        
        try:
            # exist_ok=True prevents an error if the folder already exists
            os.makedirs(folder_path, exist_ok=True)
            print(f"✅ Created/Verified: {folder}")
        except Exception as e:
            print(f"❌ Error creating {folder}: {e}")

    print("\nFolder structure setup complete!")

if __name__ == "__main__":
    # You can change "." to a specific path if needed, e.g., "C:/MyProject"
    create_project_folders(".")