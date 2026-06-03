import google.generativeai as genai
import api_keys

#API KEY loaded from api_keys.py
genai.configure(api_key=api_keys.GEMINI_API_KEY)

# Tools Definitions
def trigger_robot_movement(animation_name: str):
    """
    Triggers a specific physical movement or animation on the robot body (ESP32-S3).
    animation_name: Can be 'nod', 'shake_head', 'wave_arm', or 'dance'.
    """
    # Your tested communication code goes here
    print(f"\n[HARDWARE COMMAND] Sending '{animation_name}' to ESP32-S3 (Robot)...")

def send_watch_alert(alert_type: str):
    """
    Sends a haptic/vibration alert to the user's fitness watch (ESP32-C3).
    alert_type: Can be 'single_buzz', 'double_buzz', or 'continuous_pulse'.
    """
    # Your tested communication code goes here
    print(f"\n[HARDWARE COMMAND] Sending '{alert_type}' to ESP32-C3 (Watch)...")

#System Instructions for Gemini
genai.configure(api_key=api_keys.GEMINI_API_KEY)

# Tell the model exactly how to behave
system_prompt = """
You are the brains of a fitness robot. Your job is to translate terminal commands or natural language from the user into hardware functions.

Follow these mapping rules strictly:
1. If the user asks to test the robot, look around, say hello, or show excitement, call `trigger_robot_movement`.
2. If the user mentions checking their watch, testing alerts, or needing a physical reminder, call `send_watch_alert`.
3. If they type a natural phrase like "give me a nudge", figure out the best tool to use (e.g., watch alert).
4. Always provide a brief, witty confirmation text back to the terminal explaining what you did.
"""

model = genai.GenerativeModel(
    model_name='gemini-1.5-flash', # Low latency for fast terminal responses
    tools=[trigger_robot_movement, send_watch_alert],
    system_instruction=system_prompt
)

# Terminal Interaction Loop
def start_terminal_controller():
    # Start an interactive chat session with auto-tool execution
    chat = model.start_chat(enable_automatic_function_calling=True)
    
    print("=" * 50)
    print("Fitness Robot Terminal Controller Activated.")
    print("Type your commands below (Type 'exit' to quit).")
    print("=" * 50)
    
    while True:
        try:
            # 1. Get input from the terminal
            user_command = input("\nYou: ")
            
            # Check for exit condition
            if user_command.strip().lower() == 'exit':
                print("Shutting down controller...")
                break
                
            if not user_command.strip():
                continue

            # 2. Pass the terminal text directly to Gemini
            response = chat.send_message(user_command)
            
            # 3. Print Gemini's verbal response
            print(f"Robot: {response.text}")
            
        except Exception as e:
            print(f"An error occurred: {e}")

if __name__ == "__main__":
    start_terminal_controller()