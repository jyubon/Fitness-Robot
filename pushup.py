import cv2
import mediapipe as mp
from mediapipe.tasks import python
from mediapipe.tasks.python import vision
import numpy as np

mp_drawing = solutions.drawing_utils
mp_pose = solutions.pose

# Function to calculate angle
def calculate_angle(a, b, c):
    a = np.array(a)  # First
    b = np.array(b)  # Mid
    c = np.array(c)  # End

    radians = np.arctan2(c[1] - b[1], c[0] - b[0]) - np.arctan2(a[1] - b[1], a[0] - b[0])
    angle = np.abs(radians * 180.0 / np.pi)

    if angle > 180.0:
        angle = 360 - angle

    return angle

# Function to count push-ups
def count_pushups(nose, shoulders, stage, counter):
    # Calculate distance between nose and shoulder midpoint 
    left, right = np.array(shoulders[0]),np.array(shoulders[1])
    shoulder_mid = (left+right)/2.0
    #calculate distance between nose and shoulder midpoint
    nose = np.array(nose)
    nose_shoulder_dist = np.linalg.norm(nose - shoulder_mid)
    
    # Push-up counter logic
    if nose_shoulder_dist < 0.2 and stage == 'down':
        stage = 'up'
    if nose_shoulder_dist > 0.4 and stage == 'up':
        stage = 'down'
        counter += 1
        print("Push-ups Count:", counter)

    return stage, counter

pushup_counter = 0
pushup_stage = 'up'

cap = cv2.VideoCapture(0)

with mp_pose.Pose(min_detection_confidence=0.5, min_tracking_confidence=0.5) as pose:
    while cap.isOpened():
        ret, frame = cap.read()

        if not ret:
            break

        image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image.flags.writeable = False

        results = pose.process(image)

        image.flags.writeable = True
        image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)

        try:
            # Extract landmarks for push-ups
            landmarks = results.pose_landmarks.landmark
            nose = [landmarks[mp_pose.PoseLandmark.NOSE.value].x, landmarks[mp_pose.PoseLandmark.NOSE.value].y]
            left_shoulder = [landmarks[mp_pose.PoseLandmark.LEFT_SHOULDER.value].x,
                             landmarks[mp_pose.PoseLandmark.LEFT_SHOULDER.value].y]
            right_shoulder = [landmarks[mp_pose.PoseLandmark.RIGHT_SHOULDER.value].x,
                              landmarks[mp_pose.PoseLandmark.RIGHT_SHOULDER.value].y]

            # Count push-ups
            pushup_stage, pushup_counter = count_pushups(nose, [left_shoulder, right_shoulder], pushup_stage, pushup_counter)

        except:
            pass

        # Render push-up counter
        cv2.putText(image, 'Push-ups Count: ' + str(pushup_counter),
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)

        mp_drawing.draw_landmarks(image, results.pose_landmarks, mp_pose.POSE_CONNECTIONS,
                                  mp_drawing.DrawingSpec(color=(245, 117, 66), thickness=2, circle_radius=2),
                                  mp_drawing.DrawingSpec(color=(245, 66, 230), thickness=2, circle_radius=2)
                                  )
        
        image = cv2.flip(image, 1)

        cv2.imshow('Mediapipe Feed', image)

        if cv2.waitKey(10) & 0xFF == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()