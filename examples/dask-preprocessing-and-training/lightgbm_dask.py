# # Distributed Dask Data Processing & LightGBM Training
# In this example, we show how you can use Kubetorch to launch a multi-node Dask cluster and use it to preprocess data and then train a LightGBM model.
# The dataset we use is a 1 year sample of the NYC Taxi Dataset, which we do some illustrative pre-processing over to show the power of Dask
# to perform distributed operations. Then, LightGBM is used to do a distributed training over the data, using the Dask cluster.
import cloudpickle
import dask.array as da

import dask.dataframe as dd
import kubetorch as kt
import lightgbm as lgb
import numpy as np

from dask.distributed import Client
from dask_ml.metrics import mean_absolute_error, mean_squared_error
from dask_ml.model_selection import train_test_split

# ## Define the LightGBM Model Trainer Class
# We use this class to encapsulate the training and define the methods for data pre-processing, training, and evaluation.
# We will send this trainer to the Dask cluster we bring up from available compute below, and create a remote instance of this class.
# This is regular undecorated Python, with regular Dask and LightGBM operations to train the model.
class LightGBMModelTrainer:
    def __init__(self):
        self.model = None
        self.dataset = None
        self.X_train = None
        self.X_test = None
        self.X_eval = None
        self.y_train = None
        self.y_test = None
        self.y_eval = None
        self.client = None
        self.mae = None
        self.mse = None
        self.features = None

    def load_client(self, ip="localhost", port="8786"):
        self.client = Client(f"tcp://{ip}:{port}")

    def load_data(self, data_path):
        self.dataset = dd.read_parquet(data_path)
        print(self.dataset.columns)

    def train_test_split(self, target_var, features=None):
        if features is None:
            features = self.dataset.columns.difference([target_var])

        X = self.dataset[features]
        y = self.dataset[target_var]

        self.X_train, self.X_test, self.y_train, self.y_test = train_test_split(
            X, y, test_size=0.3, shuffle=False
        )
        self.X_eval, self.X_train, self.y_eval, self.y_train = train_test_split(
            self.X_test, self.y_test, test_size=0.5, shuffle=False
        )
        self.features = features

    def preprocess(self, date_column):
        self.dataset["day"] = self.dataset[date_column].dt.day
        self.dataset["month"] = self.dataset[date_column].dt.month
        self.dataset["dayofweek"] = self.dataset[date_column].dt.dayofweek
        self.dataset["hour"] = self.dataset[date_column].dt.hour
        return ["day", "month", "dayofweek", "hour"]

    def train_model(self):
        self.load_client()
        self.model = lgb.DaskLGBMRegressor(client=self.client)
        self.model.fit(
            self.X_train.to_dask_array(),
            self.y_train.to_dask_array(),
            eval_set=[(self.X_eval.to_dask_array(), self.y_eval.to_dask_array())],
        )
        print("Model trained successfully.", self.model)

    def test_model(self):
        self.load_client()
        if self.model is None:
            raise Exception("Model not trained yet. Please train the model first.")
        if self.X_test is None or self.y_test is None:
            raise Exception(
                "Test data not loaded yet. Please load the test data first."
            )

        y_pred = self.model.predict(self.X_test)
        y_test = self.y_test.to_dask_array().rechunk(y_pred.chunks)

        self.mae = mean_absolute_error(y_test, y_pred)
        print(f"Mean Absolute Error: {self.mae}")

        self.mse = mean_squared_error(y_test, y_pred)
        print(f"Mean Squared Error: {self.mse}")

    def return_model_details(self):
        return {"features": self.features, "mse": self.mse, "mae": self.mae}

    def save_model(self, path, upload_to_s3=False):
        if path.startswith("s3://"):
            import s3fs

            with s3fs.S3FileSystem() as s3:
                with s3.open(path, "wb") as f:
                    cloudpickle.dump(self.model, f)
        elif path.startswith("gs://"):
            import gcsfs

            fs = gcsfs.GCSFileSystem()
            with fs.open(path, "wb") as f:
                cloudpickle.dump(self.model, f)
        else:
            # Save to disk
            with open(path, "wb") as f:
                cloudpickle.dump(self.model, f)

    def predict(self, X):
        if isinstance(X, dd.DataFrame):  # Check for Dask DataFrame
            result = self.model.predict(X.to_dask_array())
        elif isinstance(X, (list, tuple)):  # Check for list or tuple
            X = da.from_array(np.array(X).reshape(1, -1), chunks=(1, 7))
            result = self.model.predict(X)
        else:
            raise ValueError("Unsupported data type for prediction.")

        return float(result.compute()[0])


# ## Launch Compute and Run the Training
# We will now launch compute with multiple nodes and use it to train a LightGBM model.
if __name__ == "__main__":
    # ## Define compute with multiple nodes
    num_nodes = 3

    # The environment for the remote compute, and the compute.
    # Importantly, we use .distribute("dask") to start the Dask cluster and indicate this will be used with Dask

    img = kt.Image(image_id="ghcr.io/dask/dask").pip_install(
        [
            "gcsfs",  # For Google Cloud Storage, change for your cloud provider
            "lightgbm",
        ]
    )
    cpus = kt.Compute(cpus="8", memory="32", image=img).distribute(
        "dask", num_nodes=num_nodes
    )

    # ## Setup the remote training
    # Now we can dispatch the model trainer class to remote, which is fully locally interactible.
    # We can call methods on it as if it were local.
    dask_trainer = kt.cls(LightGBMModelTrainer).to(cpus)
    dask_trainer.compute.ssh_tunnel(
        "8787", "8787"
    )  # Forward the Dask dashboard to local

    # Now we call the remote model trainer to do the training.
    data_path = "gs://rh-demo-external/taxi_parquet"  # NYC Taxi Data
    X_vars = ["passenger_count", "trip_distance", "fare_amount"]
    y_var = "tip_amount"

    dask_trainer.load_client()
    dask_trainer.load_data(data_path)
    new_date_columns = dask_trainer.preprocess(date_column="tpep_pickup_datetime")
    X_vars = X_vars + new_date_columns
    dask_trainer.train_test_split(target_var=y_var, features=X_vars)

    dask_trainer.train_model()
    print("Model trained")
    dask_trainer.test_model()
    print("Model tested")
    dask_trainer.save_model("gs://rh-model-checkpoints/lightgbm_dask/model.pkl")
    print("Model saved")
