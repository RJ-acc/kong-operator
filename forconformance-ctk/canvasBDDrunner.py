import subprocess
import tempfile
import shutil
import os
import json
import platform
import shutil

def find_npm_executable():
    """
    Finding the npm executable path depending on the OS (Windows or linux/macOS).
    """
    system_name = platform.system().lower()
    
    if "windows" in system_name:
        possible_executables = ["npm.cmd", "npm.exe"]
    else:
        possible_executables = ["npm"]
    
    for exe in possible_executables:
        npm_path = shutil.which(exe)
        if npm_path:
            print(f"Found npm executable at: {npm_path}")
            return npm_path

    raise FileNotFoundError(
        "npm executable not found. "
        "Make sure to install Node.js/npm or update your PATH so that 'npm' is discoverable by canvasBDDrunner"
    )

def main():
    npm_path = find_npm_executable()

    #Loading data from CanvasBDD.json before cloning repo_url & branch (default points to oda-canvas repo and main branch)
    try:
        with open('CanvasBDD.json', 'r') as file:
            canvas_data = json.load(file)
    except FileNotFoundError:
        raise FileNotFoundError("CanvasBDD.json not found. Please sure to provide the JSON file.")

    repo_url = canvas_data.get("repo_url", "https://github.com/tmforum-oda/oda-canvas.git")
    branch = canvas_data.get("branch", "main")

    #Create temporary directory for cloning repo content
    temp_dir = tempfile.mkdtemp()

    try:
        print(f"Cloning {repo_url} (branch: {branch}) into {temp_dir} ...")
        subprocess.run(["git", "clone", "-b", branch, repo_url, temp_dir], check=True)

        #Directories where to run npm install
        directories = [
            "feature-definition-and-test-kit/identity-manager-utils-keycloak",
            "feature-definition-and-test-kit/package-manager-utils-helm",
            "feature-definition-and-test-kit/resource-inventory-utils-kubernetes",
            "feature-definition-and-test-kit"  #This is root for feature-definition-and-test-kit
        ]

        #Installing npm dependencies in each directory
        for directory in directories:
            full_path = os.path.join(temp_dir, directory)
            print(f"\nRunning npm install in: {full_path}")
            print("Files in directory:", os.listdir(full_path))  #this is for optional debug 
            subprocess.run([npm_path, "install"], cwd=full_path, check=True)

        #Directory which the BDD tests
        BDD_test_dir = os.path.join(
            temp_dir,
            "feature-definition-and-test-kit",
            "features"
        )

        #Environment variable in CanvasBDD.json for KeycloakIDM
        env_data = canvas_data.get("env", {})
        env_vars = {
            "KEYCLOAK_USER": env_data.get("KEYCLOAK_USER", "admin"),
            "KEYCLOAK_PASSWORD": env_data.get("KEYCLOAK_PASSWORD", "admin"),
            "KEYCLOAK_BASE_URL": env_data.get("KEYCLOAK_BASE_URL", "http://keycloak-ip:8083/auth/"),
            "KEYCLOAK_REALM": env_data.get("KEYCLOAK_REALM", "odari"),
            "PATH": os.environ.get("PATH", "")
        }

        #Prompt for test type from user
        while True:
            user_input = input("\nEnter 'mandatory' to run mandatory BDDs, 'optional' for optional BDDs, or 'exit' to quit: ").strip().lower()

            if user_input == "exit":
                print("Exiting the program.")
                return  
            elif user_input in ["mandatory", "optional"]:
                test_type = user_input
                break  
            else:
                print("Invalid input. Try again. (Valid options are: 'mandatory', 'optional', or 'exit')")

        #Continuing tests if not exiting
        features = canvas_data.get(test_type, [])
        if not features:
            print(f"No features found for '{test_type}' in CanvasBDD.json.")
        else:
            print(f"\nRunning {test_type} test(s)...")
            for feature in features:
                print(f"Executing '{feature}' in directory: {BDD_test_dir}")
                subprocess.run(
                    [npm_path, "start", "--", f"features/{feature}"],
                    cwd=BDD_test_dir,
                    env=env_vars,
                    check=True
                )

    except subprocess.CalledProcessError as e:
        print(f"An error occurred while running a subprocess: {e}")
    except FileNotFoundError as e:
        print(f"An error occurred: {e}")
    finally:
        #Clean up of the temporary directory
        shutil.rmtree(temp_dir, ignore_errors=True)
        print("\nAll npm installations completed and temporary files cleaned up.")

if __name__ == "__main__":
    main()
