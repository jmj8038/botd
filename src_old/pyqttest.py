# SURF2017
# File: pyqttest
# Created: 8/2/17
# Author: Stephanie Ding

from __future__ import division
import sys
import operator
import FlowParser as fp
from PyQt4 import QtGui, QtCore
import numpy as np
import pyqtgraph as pg
import os
import glob
import threading
import time
from sklearn.externals import joblib
from sklearn.cluster import AgglomerativeClustering
from sklearn.preprocessing import StandardScaler

MODELS_FOLDER = "../models/"
FEATURES_LIST = fp.ARGUS_FIELDS

OFFLINE_FOLDER = 0
OFFLINE_PCAP = 1
ONLINE = 2

ALPHA = 0.2

class Model:
    model = ''
    filename = ''
    features_list = ''

    hosts_prefix = ''

    total_botnet = 0

    botnet_hosts = {}
    clusters = {}
    # botnet_dsts = {}

    # botnet_flows = []
    # dbscan = {}

    botnet_flows = set()

    def __init__(self, model_filename, features_list, hosts_prefix):
        self.filename = model_filename
        self.model = joblib.load(model_filename)
        self.features_list = features_list
        self.hosts_prefix = hosts_prefix

    def reset(self):
        self.total_botnet = 0

        for i in self.botnet_hosts:
            self.botnet_hosts[i] = {'count': 0, 'srcpkts': 0, 'dstpkts': 0, 'srcbytes': 0, 'dstbytes': 0,
                                    'unique_ports' : set(), 'unique_dsts': set()}

        # for i in self.botnet_dsts:
        #     self.botnet_dsts[i] = 0

    def predict(self, flows, xs):
        self.reset()

        y_pred = self.model.predict(xs)

        for flow, x, y in zip(flows, xs, y_pred):
            if y == 1:
                self.total_botnet += 1
                src, dst = fp.get_src_dst(flow)

                if src.startswith(self.hosts_prefix):
                    if src not in self.botnet_hosts:
                        self.botnet_hosts[src] = {}
                        self.botnet_hosts[src]['count'] = 1
                        self.botnet_hosts[src]['srcpkts'] = x[2]
                        self.botnet_hosts[src]['dstpkts'] = x[3]
                        self.botnet_hosts[src]['srcbytes'] = x[4]
                        self.botnet_hosts[src]['dstbytes'] = x[5]
                        self.botnet_hosts[src]['unique_ports'] = {flow[3]}
                        self.botnet_hosts[src]['unique_dsts'] = {dst}
                    else:
                        self.botnet_hosts[src]['count'] += 1
                        self.botnet_hosts[src]['srcpkts'] += x[2]
                        self.botnet_hosts[src]['dstpkts'] += x[3]
                        self.botnet_hosts[src]['srcbytes'] += x[4]
                        self.botnet_hosts[src]['dstbytes'] += x[5]
                        self.botnet_hosts[src]['unique_ports'].add(flow[3])
                        self.botnet_hosts[src]['unique_dsts'].add(dst)

                if dst.startswith(self.hosts_prefix):
                    if dst not in self.botnet_hosts:
                        self.botnet_hosts[dst] = {}
                        self.botnet_hosts[dst]['count'] = 1
                        self.botnet_hosts[dst]['srcpkts'] = x[3]
                        self.botnet_hosts[dst]['dstpkts'] = x[2]
                        self.botnet_hosts[dst]['srcbytes'] = x[5]
                        self.botnet_hosts[dst]['dstbytes'] = x[4]
                        self.botnet_hosts[dst]['unique_ports'] = {flow[1]}
                        self.botnet_hosts[dst]['unique_dsts'] = {src}
                    else:
                        self.botnet_hosts[dst]['count'] += 1
                        self.botnet_hosts[dst]['srcpkts'] += x[3]
                        self.botnet_hosts[dst]['dstpkts'] += x[2]
                        self.botnet_hosts[dst]['srcbytes'] += x[5]
                        self.botnet_hosts[dst]['dstbytes'] += x[4]
                        self.botnet_hosts[dst]['unique_ports'].add(flow[1])
                        self.botnet_hosts[dst]['unique_dsts'].add(src)


    def cluster(self):
        ips = []
        new_xs = []

        for host in self.botnet_hosts:
            ips.append(host)
            curr = self.botnet_hosts[host]
            new_xs.append([curr['count'], curr['srcpkts'], curr['dstpkts'], curr['srcbytes'], curr['dstbytes'],
                           len(curr['unique_ports']) / 65535, len(curr['unique_dsts'])])

        # Hierarchical clustering into 2 groups
        scaled_xs = StandardScaler().fit_transform(new_xs)
        db = AgglomerativeClustering().fit(scaled_xs)

        labels = db.labels_

        # host_vectors = {}

        clusters = {}

        for host, x, label in zip(ips, scaled_xs, labels):
            if label not in clusters:
                clusters[label] = {host}
                # host_vectors[label] = [x]
            else:
                clusters[label].add(host)
                # host_vectors[label].append(x)

        if len(clusters[0]) > len(clusters[1]): # let 0 be the majority group and 1 be the anomalous
            self.clusters = clusters
        else:
            self.clusters[0] = clusters[1]
            self.clusters[1] = clusters[0]


    def get_botnet_flows_count(self):
        return {host: self.botnet_hosts[host]['count'] for host in self.botnet_hosts}

    def get_clusters(self):
        return self.clusters

   # def get_botnet_dsts(self):
        # return self.botnet_dsts


'''
Launcher window that asks user if they want to perform real-time .pcap capture or process from file.
'''
class Launcher(QtGui.QWidget):
    def __init__(self):
        super(Launcher, self).__init__()
        self.initUI()

    def initUI(self):
        self.setGeometry(300, 300, 250, 150)
        self.setWindowTitle('Launcher')
        self.setWindowIcon(QtGui.QIcon('../etc/favicon.png'))

        self.center()
        self.show()

    def center(self):
        qr = self.frameGeometry()
        cp = QtGui.QDesktopWidget().availableGeometry().center()
        qr.moveCenter(cp)
        self.move(qr.topLeft())


'''
Thread for background processing of stuff
'''
class WorkerThread(pg.QtCore.QThread):
    owd = ''
    features_list = ''
    models_folder = ''

    models = {}

    internal_hosts_prefix = ''
    capture_folder = ''
    window_id = 0

    data = {}
    hosts_ranking = {}

    models_loaded_signal = pg.QtCore.Signal(object)
    hosts_updated_signal = pg.QtCore.Signal(object)
    data_signal = pg.QtCore.Signal(object)

    mode = 0
    offline_folder = ''
    offline_pcap = ''
    network_interface = ''

    def __init__(self, models_folder, features_list, window_length, overlap_length, internal_hosts_prefix):
        super(WorkerThread, self).__init__()
        self.stop_mutex = threading.Lock()
        self._stop = False

        self.owd = os.getcwd()

        self.window_length = window_length
        self.overlap_length = overlap_length
        self.internal_hosts_prefix = internal_hosts_prefix

        self.models_folder = models_folder
        self.features_list = features_list

    '''
    Loads all models in the folder
    '''
    def load_models(self, models_folder):
        model_names = []

        print("Loading all models in: " + models_folder)
        model_id = 1
        os.chdir(models_folder)
        for model_fname in glob.glob("*.pkl"):
            model_names.append(str(model_id) + ": " + model_fname)

            print("+ " + model_fname)
            model = Model(model_fname, self.features_list, self.internal_hosts_prefix)
            self.models[model_id] = model
            self.data[model_id] = {}
            model_id += 1
        print("Loaded " + str(model_id - 1) + " models")
        os.chdir(self.owd)

        self.models_loaded_signal.emit(tuple(model_names))

    '''
    Set session mode
    '''
    def set_mode(self, mode, pcap_folder='', pcap_file='', network_interface=''):
        self.mode = mode
        self.pcap_folder = pcap_folder
        self.pcap_file = pcap_file
        self.network_interface = network_interface

    '''
    Thread main loop
    '''
    def run(self):
        self.load_models(self.models_folder)

        # If running in offline + folder mode
        if self.mode == OFFLINE_FOLDER:
            assert(self.pcap_folder != '') # make sure pcap folder is not uninitialised

            print("\nBeginning offline session on folder: " + self.pcap_folder)

            self.window_id = 1
            os.chdir(self.pcap_folder)

            while True:
                # Must protect self._stop with a mutex because the main thread
                # might try to access it at the same time.
                with self.stop_mutex:
                    if self._stop:
                        # causes run() to exit, which kills the thread.
                        break

                    current_fname = str(self.window_id) + ".binetflow"
                    print(current_fname)

                    # If is a valid file
                    if os.path.isfile(current_fname):
                        flows, xs = fp.parse_binetflow(current_fname, self.features_list)

                        # Go through each of the models and run prediction, get output
                        for model_id in range(1, len(self.models) + 1):
                            model = self.models[model_id]
                            model.predict(flows, xs)
                            botnet_flows_count = model.get_botnet_flows_count()

                            window_count = self.window_id - 1
                            for host in botnet_flows_count:
                                old_avg = 0

                                # Add to the time series
                                if host not in self.data[model_id]:
                                    self.data[model_id][host] = {}
                                    self.data[model_id][host]['avg'] = botnet_flows_count[host]
                                    self.data[model_id][host]['series'] = [0] * window_count + [botnet_flows_count[host]]
                                else:
                                    t_prev = self.data[model_id][host]['series'][-1]
                                    t_now = botnet_flows_count[host]
                                    # self.data[model_id][host]['series'].append(botnet_hosts[host])

                                    # Exponential smoothing
                                    self.data[model_id][host]['series'].append(ALPHA * t_now + (1 - ALPHA) * t_prev)

                                    # Update the average
                                    old_avg = self.data[model_id][host]['avg']
                                    self.data[model_id][host]['avg'] = (old_avg * (window_count - 1) + botnet_flows_count[host]) / window_count

                                # Update the ranking
                                model.cluster()
                                # clusters = model.get_clusters()
                                # total = model.total_botnet
                                #
                                # for host in clusters[0]:
                                #     if host not in self.hosts_ranking:
                                #         self.hosts_ranking[host] = 0
                                #
                                # for host in clusters[1]:
                                #     if host not in self.hosts_ranking:
                                #         self.hosts_ranking[host] = 1
                                #     else:
                                #         self.hosts_ranking[host] += (0.7/len(self.models))

                                # if old_avg == 0:
                                #     scaling = 1
                                # else:
                                #     scaling = (botnet_flows_count[host] - old_avg) / old_avg
                                #     if scaling < 0:
                                #         scaling = 0
                                #
                                # if host not in self.hosts_ranking:
                                #     self.hosts_ranking[host] = (botnet_flows_count[host] / total) * scaling
                                #     # self.hosts_updated_signal.emit(self.hosts_ranking)
                                # else:
                                #     self.hosts_ranking[host] += (botnet_flows_count[host] / total) * scaling

                        clusters = self.models[7].get_clusters()

                        for host in clusters[0]:
                            if host not in self.hosts_ranking:
                                self.hosts_ranking[host] = 0.0
                            else:
                                self.hosts_ranking[host] *= 0.7

                        for host in clusters[1]:
                            if host not in self.hosts_ranking:
                                self.hosts_ranking[host] = 1.0
                            else:
                                self.hosts_ranking[host] *= 0.7
                                self.hosts_ranking[host] += 0.3

                        self.data_signal.emit(self.data)
                        self.hosts_updated_signal.emit(self.hosts_ranking)
                        self.window_id += 1
                    else:
                        # append 0 before it ends TODO
                        break

                # time.sleep(0.05)

            os.chdir(self.owd)

    '''
    Stop thread
    '''
    def stop(self):
        # Must protect self._stop with a mutex because the secondary thread
        # might try to access it at the same time.
        with self.stop_mutex:
            self._stop = True


'''
Main window displaying the graphs.
'''
class MainApplication(QtGui.QWidget):
    data = ''

    def __init__(self):
        # Initialise multithreading, worker thread for background processing
        thread = WorkerThread(MODELS_FOLDER, FEATURES_LIST, 300, 150, "147.32")
        thread.set_mode(OFFLINE_FOLDER, pcap_folder="/media/SURF2017/CTU-13-Dataset/9/capture20110817pcaptruncated_300_150")

        # When models are loaded signal main UI to update dropdown
        thread.models_loaded_signal.connect(self.update_models_dropdown)

        # Whenever new hosts are found signal main UI to update dropdown
        thread.hosts_updated_signal.connect(self.update_table)

        # Whenever new graph points are received signal main UI to update graph
        thread.data_signal.connect(self.update)

        # Begin the worker thread
        thread.start()

        super(MainApplication, self).__init__()
        self.initUI()

    def initUI(self):
        self.setGeometry(10, 10, 1000, 800)
        self.setWindowTitle('Botnet detector')
        self.setWindowIcon(QtGui.QIcon('../etc/favicon.png'))

        # Text labels
        l1 = QtGui.QLabel("Model")
        l2 = QtGui.QLabel("Network hosts")

        # Dropdown menus
        self.models_dropdown = QtGui.QComboBox(self)
        self.models_dropdown.setMinimumContentsLength(15)
        self.models_dropdown.setSizeAdjustPolicy(QtGui.QComboBox.AdjustToContents)
        self.models_dropdown.activated[str].connect(self.change_models)

        self.hosts_dropdown = QtGui.QComboBox(self)
        self.hosts_dropdown.setMinimumContentsLength(15)
        self.hosts_dropdown.setSizeAdjustPolicy(QtGui.QComboBox.AdjustToContents)
        self.hosts_dropdown.activated[str].connect(self.change_hosts)

        # Plot widget
        self.plotwidget = pg.PlotWidget()
        self.plotwidget.setLimits(xMin=0, yMin=0)

        # Table
        self.table = QtGui.QTableWidget()
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(QtCore.QString("host;score;").split(";"))
        self.table.horizontalHeader().setResizeMode(0, QtGui.QHeaderView.Stretch)
        self.table.setEditTriggers(QtGui.QAbstractItemView.NoEditTriggers)
        self.table.setDragDropOverwriteMode(False)
        self.table.setDragDropMode(QtGui.QAbstractItemView.NoDragDrop)
        self.table.setSelectionMode(QtGui.QAbstractItemView.ExtendedSelection)
        self.table.setSelectionBehavior(QtGui.QAbstractItemView.SelectRows)

        # Set layout
        self.hbox = QtGui.QHBoxLayout()
        self.vbox = QtGui.QVBoxLayout()

        self.vbox.addWidget(l1)
        self.vbox.addWidget(self.models_dropdown)
        self.vbox.addStretch(1)
        self.vbox.addWidget(l2)
        self.vbox.addWidget(self.hosts_dropdown)
        self.vbox.addStretch(2)

        self.hbox.addWidget(self.plotwidget)
        self.hbox.addLayout(self.vbox)

        self.vbox2 = QtGui.QVBoxLayout()
        self.vbox2.addLayout(self.hbox, 2)
        self.vbox2.addWidget(self.table, 1)

        self.setLayout(self.vbox2)

        self.center()
        self.show()

    def closeEvent(self, event):
        reply = QtGui.QMessageBox.question(self, 'Message',
                                           "Are you sure to quit?", QtGui.QMessageBox.Yes |
                                           QtGui.QMessageBox.No, QtGui.QMessageBox.No)

        if reply == QtGui.QMessageBox.Yes:
            event.accept()
        else:
            event.ignore()

    def center(self):
        qr = self.frameGeometry()
        cp = QtGui.QDesktopWidget().availableGeometry().center()
        qr.moveCenter(cp)
        self.move(qr.topLeft())

    def update_table(self, hosts_ranking):
        row_count = self.table.rowCount()
        row_diff = len(hosts_ranking) - row_count
        if row_diff > 0:
            for i in range(row_count, row_count + row_diff):
                self.table.insertRow(i)

        row = 0
        for host, score in sorted(hosts_ranking.items(), key=operator.itemgetter(1), reverse=True):
            if host == 'ALL':
                continue

            self.table.setItem(row, 0, QtGui.QTableWidgetItem(host))
            self.table.setItem(row, 1, QtGui.QTableWidgetItem("{:.4f}".format(score)))
            row += 1

    def update_hosts_dropdown(self):
        selected_model = int(str(self.models_dropdown.currentText().split(":")[0]))
        self.hosts_dropdown.clear()
        for host in sorted(self.data[selected_model].keys()):
            self.hosts_dropdown.addItem(host)

    def update_models_dropdown(self, models):
        for model_name in models:
            self.models_dropdown.addItem(model_name)

    def change_hosts(self, text):
        selected_model = int(str(self.models_dropdown.currentText().split(":")[0]))
        selected_host = str(text)
        curve = self.data[selected_model][selected_host]['series']
        self.plotwidget.plot(curve, clear=True)

    def change_models(self, text):
        selected_model = int(str(text.split(":")[0]))
        print("selected model is: " + str(selected_model))
        self.update_hosts_dropdown()

        # selected_host = str(self.hosts_dropdown.currentText())
        # if selected_host != '':
        #     curve = self.data[selected_model][selected_host]
        #     self.plotwidget.plot(curve, clear=True)

    def update(self, data):
        self.data = data
        selected_model = int(str(self.models_dropdown.currentText().split(":")[0]))
        prev_selected_host = str(self.hosts_dropdown.currentText())
        self.update_hosts_dropdown()

        if prev_selected_host != '':
            index = self.hosts_dropdown.findText(prev_selected_host, QtCore.Qt.MatchFixedString)
            if index >= 0:
                self.hosts_dropdown.setCurrentIndex(index)
                selected_host = prev_selected_host
            else:
                selected_host = str(self.hosts_dropdown.currentText())
        else:
            selected_host = str(self.hosts_dropdown.currentText())

        curve = self.data[selected_model][selected_host]['series']

        self.plotwidget.plot(curve, clear=True)
        # self.plotwidget.setXRange(x1, x2)

'''
Main function
'''
def main():
    app = QtGui.QApplication(sys.argv)
    # launcher = Launcher()
    main_app = MainApplication()
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
