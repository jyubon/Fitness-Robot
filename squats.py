import cv2
import mediapipe as mp
import numpy as np
import time



def calculate_angle_3d(a, b, c):
    """Calculates the true angle (in degrees) between three 3D joint landmarks (a-b-c)."""
    # a, b, c are lists of [x, y, z] coordinates
    a = np.array(a)  # Start point (Shoulder)
    b = np.array(b)  # Mid point (Hip)
    c = np.array(c)  # End point (Knee)

    # Calculate vectors
    ba = a - b
    bc = c - b

    
    cosine_angle = np.dot(ba, bc) / (np.linalg.norm(ba) * np.linalg.norm(bc))
    
    
    angle = np.arccos(np.clip(cosine_angle, -1.0, 1.0))


    return np.degrees(angle)




def detect_squats():
    """
    Detects squats using pose estimation and returns the rep count and minimum angle achieved.
    
    Returns:
        tuple: (rep_count, rep_angle) - Number of reps completed and minimum angle achieved
    """
    mp_drawing = mp.solutions.drawing_utils
    mp_pose = mp.solutions.pose

    counter = 0      
    stage = "up"     
    SQUAT_DOWN_ANGLE = 110 
    SQUAT_UP_ANGLE = 160   
    down_angle = SQUAT_DOWN_ANGLE  

    cap = cv2.VideoCapture(0) 
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    print("Starting All-Angle Squat Detection...")

    with mp_pose.Pose(min_detection_confidence=0.5, min_tracking_confidence=0.5) as pose:
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                print("Failed to grab frame.")
                break

            # RGB to BGR
            image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image.flags.writeable = False
            results = pose.process(image)
            image.flags.writeable = True
            image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)

            current_hip_angle = 180 

            posture_correct = False

            try:
                landmarks = results.pose_landmarks.landmark
                
                L_shoulder = [landmarks[mp_pose.PoseLandmark.LEFT_SHOULDER].x, landmarks[mp_pose.PoseLandmark.LEFT_SHOULDER].y, landmarks[mp_pose.PoseLandmark.LEFT_SHOULDER].z]
                L_hip = [landmarks[mp_pose.PoseLandmark.LEFT_HIP].x, landmarks[mp_pose.PoseLandmark.LEFT_HIP].y, landmarks[mp_pose.PoseLandmark.LEFT_HIP].z]
                L_knee = [landmarks[mp_pose.PoseLandmark.LEFT_KNEE].x, landmarks[mp_pose.PoseLandmark.LEFT_KNEE].y, landmarks[mp_pose.PoseLandmark.LEFT_KNEE].z]

                R_shoulder = [landmarks[mp_pose.PoseLandmark.RIGHT_SHOULDER].x, landmarks[mp_pose.PoseLandmark.RIGHT_SHOULDER].y, landmarks[mp_pose.PoseLandmark.RIGHT_SHOULDER].z]
                R_hip = [landmarks[mp_pose.PoseLandmark.RIGHT_HIP].x, landmarks[mp_pose.PoseLandmark.RIGHT_HIP].y, landmarks[mp_pose.PoseLandmark.RIGHT_HIP].z]
                R_knee = [landmarks[mp_pose.PoseLandmark.RIGHT_KNEE].x, landmarks[mp_pose.PoseLandmark.RIGHT_KNEE].y, landmarks[mp_pose.PoseLandmark.RIGHT_KNEE].z]

                L_hip_angle = calculate_angle_3d(L_shoulder, L_hip, L_knee)
                R_hip_angle = calculate_angle_3d(R_shoulder, R_hip, R_knee)
                
                current_hip_angle = min(L_hip_angle, R_hip_angle)
                
                if current_hip_angle < SQUAT_DOWN_ANGLE:
                    stage = "down"
                    if down_angle > current_hip_angle:
                        down_angle = current_hip_angle
                    else:
                        down_angle = down_angle

                if current_hip_angle > SQUAT_UP_ANGLE and stage == 'down':
                    stage = "up"
                    if down_angle > 80:
                        counter += 1
                        print(f"Rep {counter} completed! Angle: {int(down_angle)}")
                    down_angle = SQUAT_DOWN_ANGLE
                
                posture_correct = stage == "up"
                
                mp_drawing.draw_landmarks(image, results.pose_landmarks, mp_pose.POSE_CONNECTIONS,
                                        mp_drawing.DrawingSpec(color=(255, 0, 0), thickness=2, circle_radius=2), 
                                        mp_drawing.DrawingSpec(color=(0, 255, 0), thickness=2, circle_radius=2)
                                        )
                
                cv2.putText(image, f'3D Hip Angle: {int(current_hip_angle)}', (300, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2, cv2.LINE_AA)
                cv2.putText(image, f'Down Angle: {int(down_angle)}', (300, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2, cv2.LINE_AA)
            except:
                pass 

            color = (0, 255, 0) if posture_correct else (0, 0, 255)
            
            cv2.rectangle(image, (0, 0), (280, 70), color, -1)
            
            cv2.putText(image, 'REPS', (15, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 1, cv2.LINE_AA)
            cv2.putText(image, str(counter), (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (255, 255, 255), 2, cv2.LINE_AA)
            
            cv2.putText(image, 'STAGE', (140, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 1, cv2.LINE_AA)
            cv2.putText(image, stage.upper(), (135, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (255, 255, 255), 2, cv2.LINE_AA)
            
            cv2.putText(image, f'Posture: {"Correct" if posture_correct else "Wrong"}', (15, 100), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 2, cv2.LINE_AA)
                   
            cv2.imshow('All-Angle Pose Tracker for Squats', image)

            if cv2.waitKey(5) & 0xFF == ord('q'):
                break

        cap.release()
        cv2.destroyAllWindows()
    
    return counter, down_angle


if __name__ == "__main__":
    rep_count, rep_angle = detect_squats()
    print(f"\nSquat Detection Complete!")
    print(f"Total Reps: {rep_count}")
    print(f"Final Rep Angle: {int(rep_angle)}")
 