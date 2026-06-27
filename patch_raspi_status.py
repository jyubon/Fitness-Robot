import sys

file_path = r"c:\Users\praba\Documents\Projects\Fitness Robot\Fitness-Robot\person_raspi.py"

with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# Helper to check if already replaced or make replacement
def replace_target(content, target, replacement, name):
    if target in content:
        content = content.replace(target, replacement)
        print(f"{name} replaced successfully.")
        return content
    else:
        # Try CRLF
        target_crlf = target.replace("\n", "\r\n")
        if target_crlf in content:
            replacement_crlf = replacement.replace("\n", "\r\n")
            content = content.replace(target_crlf, replacement_crlf)
            print(f"{name} replaced successfully (CRLF).")
            return content
        else:
            print(f"Error: {name} target not found!")
            sys.exit(1)

# ─── 1. PUSHUPS STATUS FIXES ───
target_pushups_init = """    positioning_completed = False
    send_command("FRONT")

    try:"""

replacement_pushups_init = """    positioning_completed = False
    send_command("FRONT")
    
    robot_status = "TRACKING"

    try:"""

content = replace_target(content, target_pushups_init, replacement_pushups_init, "Pushups Init")

target_pushups_adjust1 = """                    if positioning_completed and not paused:
                        adjust_robot_position("PUSH_UPS", results)"""

replacement_pushups_adjust1 = """                    if positioning_completed and not paused:
                        robot_status = adjust_robot_position("PUSH_UPS", results)
                        if robot_status == "LOST_TIMEOUT":
                            break"""

content = replace_target(content, target_pushups_adjust1, replacement_pushups_adjust1, "Pushups Adjust 1")

target_pushups_adjust2 = """                    if positioning_completed and not paused:
                        adjust_robot_position("PUSH_UPS", None)"""

replacement_pushups_adjust2 = """                    if positioning_completed and not paused:
                        robot_status = adjust_robot_position("PUSH_UPS", None)"""

content = replace_target(content, target_pushups_adjust2, replacement_pushups_adjust2, "Pushups Adjust 2")

# ─── 2. SQUATS STATUS FIXES ───
target_squats_init = """    positioning_completed = False
    send_command("FRONT")

    try:"""

# Note: this targets the first one, which was pushups. But we already replaced pushups target to have robot_status inside it,
# so the exact string "positioning_completed = False\n    send_command(\"FRONT\")\n\n    try:" will now only match squats!
content = replace_target(content, target_squats_init, replacement_pushups_init, "Squats Init")

# Let's add adjust_robot_position(results) in squats
target_squats_adjust1 = """            try:
                if results.pose_landmarks and len(results.pose_landmarks) > 0:
                    lm = results.pose_landmarks[0]"""

replacement_squats_adjust1 = """            try:
                if results.pose_landmarks and len(results.pose_landmarks) > 0:
                    if positioning_completed and not paused:
                        robot_status = adjust_robot_position("SQUATS", results)
                        if robot_status == "LOST_TIMEOUT":
                            break
                    lm = results.pose_landmarks[0]"""

content = replace_target(content, target_squats_adjust1, replacement_squats_adjust1, "Squats Adjust 1")

target_squats_adjust2 = """                    if positioning_completed and not paused:
                        adjust_robot_position("SQUATS", None)"""

replacement_squats_adjust2 = """                    if positioning_completed and not paused:
                        robot_status = adjust_robot_position("SQUATS", None)"""

content = replace_target(content, target_squats_adjust2, replacement_squats_adjust2, "Squats Adjust 2")

# ─── 3. PLANK STATUS FIXES ───
target_plank_init = """    positioning_completed = False
    send_command("SIDE")

    try:"""

replacement_plank_init = """    positioning_completed = False
    send_command("SIDE")
    
    robot_status = "TRACKING"

    try:"""

content = replace_target(content, target_plank_init, replacement_plank_init, "Plank Init")

target_plank_adjust1 = """                    if positioning_completed and not paused:
                        adjust_robot_position("PLANK", results)"""

replacement_plank_adjust1 = """                    if positioning_completed and not paused:
                        robot_status = adjust_robot_position("PLANK", results)
                        if robot_status == "LOST_TIMEOUT":
                            break"""

content = replace_target(content, target_plank_adjust1, replacement_plank_adjust1, "Plank Adjust 1")

target_plank_adjust2 = """                    if positioning_completed and not paused:
                        adjust_robot_position("PLANK", None)"""

replacement_plank_adjust2 = """                    if positioning_completed and not paused:
                        robot_status = adjust_robot_position("PLANK", None)"""

content = replace_target(content, target_plank_adjust2, replacement_plank_adjust2, "Plank Adjust 2")

# Save file
with open(file_path, "w", encoding="utf-8") as f:
    f.write(content)

print("Patching successful.")
