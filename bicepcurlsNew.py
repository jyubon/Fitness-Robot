import cv2
import mediapipe as mp
import numpy as np

mp_drawing = mp.solutions.drawing_utils
mp_pose = mp.solutions.pose

def calculate_angle(a, b, c):
    a, b, c = np.array(a), np.array(b), np.array(c)
    radians = np.arctan2(c[1] - b[1], c[0] - b[0]) - np.arctan2(a[1] - b[1], a[0] - b[0])
    angle = np.abs(radians * 180.0 / np.pi)
    return 360 - angle if angle > 180.0 else angle

def process_arm(shoulder, elbow, wrist, side_name, state_dict, image, img_w, img_h):
    if shoulder is None or elbow is None or wrist is None:
        return "N/A", (0, 0, 255)

    angle = calculate_angle(shoulder, elbow, wrist)
    
    # 1. Elbow Flare Detection (Form Correction)
    # If the elbow horizontal position deviates too much from the shoulder
    flare_error = abs(shoulder[0] - elbow[0]) > 0.1  # Threshold for "swinging" out
    
    # 2. Logic for Counting
    color = (0, 255, 0) # Default Green
    feedback = "GOOD"

    if flare_error:
        feedback = "TUCK ELBOW"
        color = (0, 165, 255) # Orange for warning

    if angle > 150:
        state_dict['stage'] = "down"
    if angle < 30 and state_dict['stage'] == "down":
        state_dict['stage'] = "up"
        if not flare_error: # Only count if form was decent
            state_dict['counter'] += 1

    # 3. Visuals: Progress Bar
    bar_val = np.interp(angle, (30, 150), (100, 0)) # 100% at 30 deg, 0% at 150 deg
    px_y = 150 if side_name == "Left" else 300
    
    cv2.rectangle(image, (580, px_y), (600, px_y + 100), (50, 50, 50), 3)
    cv2.rectangle(image, (580, px_y + 100 - int(bar_val)), (600, px_y + 100), color, -1)
    cv2.putText(image, side_name[0], (585, px_y - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

    return feedback, color

# Initialize counters for both sides
arms = {
    'left': {'counter': 0, 'stage': 'down'},
    'right': {'counter': 0, 'stage': 'down'}
}

cap = cv2.VideoCapture(0)
cv2.namedWindow('AI Personal Trainer', cv2.WINDOW_NORMAL)
cv2.resizeWindow('AI Personal Trainer', 1280, 720)  # Set your desired width and height here

with mp_pose.Pose(min_detection_confidence=0.7, min_tracking_confidence=0.7) as pose:
    while cap.isOpened():
        ret, frame = cap.read()
        if not ret: break

        frame = cv2.flip(frame, 1) # Mirror for natural movement
        img_h, img_w, _ = frame.shape
        image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        results = pose.process(image)
        image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)

        if results.pose_landmarks:
            lm = results.pose_landmarks.landmark
            
            def get_pt(idx):
                p = lm[idx]
                return [p.x, p.y] if p.visibility > 0.5 else None

            # Get points
            l_s, l_e, l_w = get_pt(11), get_pt(13), get_pt(15)
            r_s, r_e, r_w = get_pt(12), get_pt(14), get_pt(16)

            # Process both arms
            l_fb, l_clr = process_arm(l_s, l_e, l_w, "Left", arms['left'], image, img_w, img_h)
            r_fb, r_clr = process_arm(r_s, r_e, r_w, "Right", arms['right'], image, img_w, img_h)

            # Draw Skeleton
            mp_drawing.draw_landmarks(image, results.pose_landmarks, mp_pose.POSE_CONNECTIONS)

            # UI Dashboard
            cv2.rectangle(image, (0, 0), (300, 100), (100, 100, 100), -1)
            
            # Left Stats
            cv2.putText(image, f"LEFT:  {arms['left']['counter']} ({l_fb})", (10, 40), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, l_clr, 2)
            # Right Stats
            cv2.putText(image, f"RIGHT: {arms['right']['counter']} ({r_fb})", (10, 80), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, r_clr, 2)

        cv2.imshow('AI Personal Trainer', image)
        if cv2.waitKey(10) & 0xFF == ord('q'): break

cap.release()
cv2.destroyAllWindows()