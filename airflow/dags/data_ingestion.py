from datetime import timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from airflow.decorators import dag, task
from airflow.utils.dates import days_ago
from email.mime.text import MIMEText
import datetime
import random
import glob
import os
import pandas as pd
import shutil
import great_expectations as gx
import logging
import smtplib

import sys
sys.path.append('/opt/api')
from models import *
from db_setup import *
from config import *

DB_URL = "postgresql://postgres:khanhduong@host.docker.internal:5432/mydbs"
user_email = "duong.tranhn1102@gmail.com"


def send_email(sender, recipient, subject, message):
    # Create the message
    message = MIMEText(message)
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient

    # Establish a connection with the SMTP server
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(user_email, "ulws pdlo avlh oggs")
        server.sendmail(sender, recipient, message.as_string())


@dag(
    dag_id='data_ingestion',
    description='Take files and validate the quality',
    tags=['dsp', 'validate', 'ingestion'],
    schedule=timedelta(minutes=1),
    start_date=days_ago(n=0, hour=1),
    catchup=False
)
def data_ingestion():
    default_folder = "/opt/data/Folder A"
    good_folder = "/opt/data/Folder C"
    failed_folder = "/opt/data/Folder B"

    @task
    def read_file():
        file_pattern = os.path.join(default_folder, "*.csv")
        file_paths = glob.glob(file_pattern)
        logging.info(f'{file_paths}')
        file_paths = [f for f in file_paths if
                      not os.path.basename(f).startswith('processed_')]

        file_path = random.choice(file_paths)
        logging.info(f'Chosen file: {file_path}')

        # Define the new name for the processed file
        processed_file_name = "processed_" + os.path.basename(file_path)
        processed_file_path = os.path.join(default_folder,
                                           processed_file_name)

        os.rename(file_path, processed_file_path)

        return processed_file_path

    @task
    def validate_data(file):
        context = gx.get_context()
        validator = context.sources.pandas_default.read_csv(file)

        validator.expect_column_values_to_be_in_set(
            "Gender", ["Male", "Female"],
            result_format={'result_format': 'SUMMARY'}
        )

        validator.expect_column_values_to_be_between(
            "Age", min_value=0, max_value=120,
            result_format={'result_format': 'SUMMARY'}
        )

        validator.expect_column_values_to_be_between(
            "Tenure", min_value=0,
            result_format={'result_format': 'SUMMARY'}
        )

        validator.expect_column_values_to_be_between(
            "CreditScore", min_value=0,
            result_format={'result_format': 'SUMMARY'}
        )

        validator.expect_column_values_to_be_between(
            "Balance", min_value=0, result_format={'result_format': 'SUMMARY'}
        )
        validator.expect_column_values_to_be_between(
            "NumOfProducts", min_value=1, max_value=5,
            result_format={'result_format': 'SUMMARY'}
        )
        validator.expect_column_values_to_be_in_set(
            "CardType", ["SILVER", "GOLD", "PLATINUM", "DIAMOND"],
            result_format={'result_format': 'SUMMARY'}
        )
        validator.expect_column_values_to_be_in_set(
            "SatisfactionScore", [1, 2, 3, 4, 5],
            result_format={'result_format': 'SUMMARY'}
        )

        validator.expect_column_values_to_be_in_set(
            "HasCrCard", [0, 1], result_format={'result_format': 'SUMMARY'}
        )

        validator.expect_column_values_to_be_in_set(
            "IsActiveMember", [0, 1],
            result_format={'result_format': 'SUMMARY'}
        )

        validator.expect_column_values_to_be_of_type(
            "EstimatedSalary", "float64",
            result_format={'result_format': 'SUMMARY'}
        )

        validator.expect_column_values_to_be_between(
            "PointEarned", min_value=0,
            result_format={'result_format': 'SUMMARY'}
        )

        validator_result = validator.validate()
        return validator_result

    @task
    def raise_alert(validator_result):
        sender = user_email
        recipient = "trankhanhduong112@gmail.com"
        subject = "Data Quality Issues"
        message = "Data quality issues detected. Check the logs for details."

        for result in validator_result["results"]:
            if not result["success"]:
                send_email(sender, recipient, subject, message)
                logging.info(f'Email sent!')

    @task
    def split_file(file, validator_result, folder_b, folder_c):
        df = pd.read_csv(file)
        problem_rows = []

        timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")

        for result in validator_result["results"]:
            if not result["success"]:
                problem_rows.extend(
                    result["result"]["partial_unexpected_index_list"])

        if not problem_rows:
            shutil.move(file, folder_c)
        else:
            df_problems = df.loc[problem_rows]
            df_no_problems = df.drop(problem_rows)

            problems_file_path = (
                os.path.join(folder_b,
                             f"file_with_problems_"
                             f"{timestamp}_{os.path.basename(file)}"))
            no_problems_file_path = (
                os.path.join(folder_c,
                             f"file_without_problems_"
                             f"{os.path.basename(file)}"))

            df_problems.to_csv(problems_file_path, index=False)
            df_no_problems.to_csv(no_problems_file_path, index=False)

    @task
    def save_log(validator_result, db_url):
        engine = create_engine(db_url)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        session = Session()

        file_name = (
            os.path.basename(validator_result
                             ["meta"]["batch_spec"]
                             ["reader_options"]["filepath_or_buffer"]))
        for result in validator_result["results"]:
            if not result["success"]:
                column = result["expectation_config"]["kwargs"]["column"]
                expectation_type = result["expectation_config"][
                    "expectation_type"]
                unexpected_values = (
                    str(result["result"]["partial_unexpected_list"]))

                stat = ProblemStats(
                    file_name=file_name,
                    column=column,
                    expectation_type=expectation_type,
                    unexpected_values=unexpected_values
                )
                session.add(stat)

            session.commit()
            session.close()

    # Task
    chosen_file = read_file()
    validate = validate_data(chosen_file)
    raise_alert(validate)
    split_file(chosen_file, validate, failed_folder, good_folder)
    save_log(validate, DB_URL)


ingestion_dag = data_ingestion()
