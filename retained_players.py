"""
Retained Players Data for IPL 2025
"""

# Retained players with their salaries (in rupees)
RETAINED_PLAYERS = {
    "CSK": [
        ("Ruturaj Gaikwad", 180000000),  # 18 Cr
        ("Ravindra Jadeja", 180000000),  # 18 Cr
        ("Matheesha Pathirana", 130000000),  # 13 Cr
        ("Shivam Dube", 120000000),  # 12 Cr
        ("MS Dhoni", 40000000),  # 4 Cr
    ],
    "DC": [
        ("Axar Patel", 165000000),  # 16.5 Cr
        ("Kuldeep Yadav", 132500000),  # 13.25 Cr
        ("Tristan Stubbs", 100000000),  # 10 Cr
        ("Abhishek Porel", 40000000),  # 4 Cr
    ],
    "GT": [
        ("Rashid Khan", 180000000),  # 18 Cr
        ("Shubman Gill", 165000000),  # 16.5 Cr
        ("Sai Sudharsan", 85000000),  # 8.5 Cr
        ("Rahul Tewatia", 40000000),  # 4 Cr
        ("Shahrukh Khan", 40000000),  # 4 Cr
    ],
    "KKR": [
        ("Rinku Singh", 130000000),  # 13 Cr
        ("Varun Chakravarthy", 120000000),  # 12 Cr
        ("Sunil Narine", 120000000),  # 12 Cr
        ("Andre Russell", 120000000),  # 12 Cr
        ("Ramandeep Singh", 40000000),  # 4 Cr
        ("Harshit Rana", 40000000),  # 4 Cr
    ],
    "LSG": [
        ("Nicholas Pooran", 210000000),  # 21 Cr
        ("Ravi Bishnoi", 110000000),  # 11 Cr
        ("Mayank Yadav", 110000000),  # 11 Cr
        ("Mohsin Khan", 40000000),  # 4 Cr
        ("Ayush Badoni", 40000000),  # 4 Cr
    ],
    "MI": [
        ("Jasprit Bumrah", 180000000),  # 18 Cr
        ("Suryakumar Yadav", 163500000),  # 16.35 Cr
        ("Hardik Pandya", 163500000),  # 16.35 Cr
        ("Rohit Sharma", 163000000),  # 16.3 Cr
        ("Tilak Varma", 80000000),  # 8 Cr
    ],
    "PBKS": [
        ("Shashank Singh", 55000000),  # 5.5 Cr
        ("Prabhsimran Singh", 40000000),  # 4 Cr
    ],
    "RR": [
        ("Sanju Samson", 180000000),  # 18 Cr
        ("Yashasvi Jaiswal", 180000000),  # 18 Cr
        ("Dhruv Jurel", 140000000),  # 14 Cr
        ("Riyan Parag", 140000000),  # 14 Cr
        ("Shimron Hetmyer", 110000000),  # 11 Cr
        ("Sandeep Sharma", 40000000),  # 4 Cr
    ],
    "RCB": [
        ("Virat Kohli", 210000000),  # 21 Cr
        ("Rajat Patidar", 110000000),  # 11 Cr
        ("Yash Dayal", 50000000),  # 5 Cr
    ],
    "SRH": [
        ("Heinrich Klaasen", 230000000),  # 23 Cr
        ("Pat Cummins", 180000000),  # 18 Cr
        ("Abhishek Sharma", 140000000),  # 14 Cr
        ("Travis Head", 140000000),  # 14 Cr
        ("Nitish Kumar Reddy", 60000000),  # 6 Cr
    ],
}


def get_total_retained_cost(team_code: str) -> int:
    """Get total cost of retained players for a team"""
    if team_code not in RETAINED_PLAYERS:
        return 0
    return sum(salary for _, salary in RETAINED_PLAYERS[team_code])


def get_remaining_purse(team_code: str, initial_purse: int) -> int:
    """Calculate remaining purse after retaining players"""
    return initial_purse - get_total_retained_cost(team_code)
